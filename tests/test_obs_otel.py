"""Tests for the vendored OTel bootstrap — the failure modes it guards are silent ones."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from types import ModuleType
from unittest import mock

import pytest

from netix_backend.observability import otel
from netix_backend.observability.logging import TRACE_ID_FIELDS

DJANGO_INSTRUMENTOR = "opentelemetry.instrumentation.django.DjangoInstrumentor"
LOGGING_INSTRUMENTOR = "opentelemetry.instrumentation.logging.LoggingInstrumentor"


@contextmanager
def module_missing(*names: str):
    """Make importing *names* raise ImportError, the way a service without the [otel] extra sees it."""
    sentinel = object()
    saved = {name: sys.modules.get(name, sentinel) for name in names}
    for name in names:
        sys.modules[name] = None
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@pytest.fixture(autouse=True)
def clean_env():
    """Isolate each test from OTEL_* leaking in from the developer's shell."""
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith(("OTEL_", "NETIX_OTEL"))}
    yield
    for k in list(os.environ):
        if k.startswith(("OTEL_", "NETIX_OTEL")):
            del os.environ[k]
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def clean_latch():
    """Reset the process latch through the public helper instead of reloading a shared library module."""
    otel.reset_for_tests()
    yield
    otel.reset_for_tests()


@pytest.fixture
def instrumented():
    """Patch everything configure() reaches for, so no global provider or instrumentation survives the test."""
    with (
        mock.patch(DJANGO_INSTRUMENTOR) as django,
        mock.patch(LOGGING_INSTRUMENTOR) as logging_i,
        mock.patch.object(otel, "_init_sdk") as init_sdk,
        mock.patch.object(otel, "_instrument_optional") as instrument_optional,
    ):
        yield mock.Mock(django=django, logging=logging_i, init_sdk=init_sdk, instrument_optional=instrument_optional)


class TestEndpointGate:
    """Default is ON per the OTel spec, but only when a collector endpoint is actually configured."""

    def test_off_without_endpoint(self, instrumented):
        assert otel.configure("svc") is False
        instrumented.django.assert_not_called()
        assert otel.is_configured() is False

    def test_on_with_endpoint(self, instrumented):
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4317"
        assert otel.configure("svc") is True
        assert otel.is_configured() is True

    def test_console_exporter_needs_no_endpoint(self, instrumented):
        os.environ["OTEL_TRACES_EXPORTER"] = "console"
        assert otel.configure("svc") is True

    def test_does_not_set_service_name_when_gated(self, instrumented):
        otel.configure("svc")
        assert "OTEL_SERVICE_NAME" not in os.environ


class TestExplicitOptOut:
    @pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "  true  "])
    def test_disabled_values(self, instrumented, value):
        os.environ["OTEL_SDK_DISABLED"] = value
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "http://collector:4317"
        assert otel.configure("svc") is False
        instrumented.django.assert_not_called()

    def test_a_false_value_leaves_tracing_on(self, instrumented):
        os.environ["OTEL_SDK_DISABLED"] = "false"
        os.environ["OTEL_TRACES_EXPORTER"] = "console"
        assert otel.configure("svc") is True


class TestEnabled:
    @pytest.fixture(autouse=True)
    def enable(self):
        """Console exporter: enabled path with no OTLP endpoint or gRPC channel in tests."""
        os.environ["OTEL_TRACES_EXPORTER"] = "console"

    def test_instruments_django_and_logging(self, instrumented):
        assert otel.configure("svc") is True
        instrumented.django.return_value.instrument.assert_called_once()
        # set_logging_format stays False, but inject_trace_context must be True or no log line gets a trace id.
        instrumented.logging.return_value.instrument.assert_called_once_with(
            set_logging_format=False, inject_trace_context=True
        )
        assert otel.is_configured() is True

    def test_log_correlation_can_be_turned_off(self, instrumented):
        os.environ["OTEL_PYTHON_LOG_CORRELATION"] = "FALSE"
        otel.configure("svc")
        instrumented.logging.return_value.instrument.assert_called_once_with(
            set_logging_format=False, inject_trace_context=False
        )

    def test_names_the_correlated_log_fields(self, instrumented, caplog):
        """The format string in logging.py and the injection here must agree; the shared constant is why."""
        with caplog.at_level(logging.INFO, logger="netix_backend.observability.otel"):
            otel.configure("svc")
        for field in TRACE_ID_FIELDS:
            assert field in caplog.text

    def test_installs_the_sdk_before_instrumenting(self, instrumented):
        """Instrumentors without a real TracerProvider only make no-op, non-recording spans."""
        otel.configure("svc")
        instrumented.init_sdk.assert_called_once_with("console")

    def test_configmap_service_name_wins(self, instrumented):
        os.environ["OTEL_SERVICE_NAME"] = "from-configmap"
        otel.configure("from-code")
        assert os.environ["OTEL_SERVICE_NAME"] == "from-configmap"

    def test_code_service_name_is_used_when_unset(self, instrumented):
        otel.configure("from-code")
        assert os.environ["OTEL_SERVICE_NAME"] == "from-code"

    def test_no_service_name_argument_leaves_the_variable_unset(self, instrumented):
        otel.configure()
        assert "OTEL_SERVICE_NAME" not in os.environ

    def test_idempotent(self, instrumented):
        """asgi.py and manage.py both call configure() unconditionally, so the second call must not re-instrument."""
        assert otel.configure("svc") is True
        assert otel.configure("svc") is False
        instrumented.django.return_value.instrument.assert_called_once()

    def test_reset_for_tests_reopens_the_latch(self, instrumented):
        assert otel.configure("svc") is True
        otel.reset_for_tests()
        assert otel.is_configured() is False
        assert otel.configure("svc") is True

    def test_db_instrumentation_is_on_by_default(self, instrumented):
        otel.configure("svc")
        instrumented.instrument_optional.assert_any_call("opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor")

    def test_db_instrumentation_can_be_disabled(self, instrumented):
        os.environ["NETIX_OTEL_DB"] = "FALSE"
        otel.configure("svc")
        modules = [call.args[0] for call in instrumented.instrument_optional.call_args_list]
        assert "opentelemetry.instrumentation.psycopg" not in modules
        assert "opentelemetry.instrumentation.redis" in modules
        assert "opentelemetry.instrumentation.httpx" in modules


class TestAsgiSupport:
    """ASGI support is an extra of the django instrumentation; missing it silently untraces every uvicorn request."""

    def test_asgi_instrumentation_ships_with_the_otel_extra(self):
        import opentelemetry.instrumentation.asgi  # noqa: F401

    def test_django_middleware_reports_asgi_supported(self):
        from opentelemetry.instrumentation.django.middleware import otel_middleware

        supported = getattr(otel_middleware, "_is_asgi_supported", None)
        if supported is None:
            pytest.skip("instrumentation no longer exposes _is_asgi_supported")
        assert supported is True

    def test_missing_asgi_support_is_loud_but_not_fatal(self, instrumented, caplog):
        os.environ["OTEL_TRACES_EXPORTER"] = "console"
        with caplog.at_level(logging.ERROR), module_missing("opentelemetry.instrumentation.asgi"):
            assert otel.configure("svc") is True
        assert "will NOT be traced" in caplog.text


class TestDegradesWithoutTheExtra:
    """The whole point of the [otel] extra: a service that skips it still imports and boots."""

    def test_module_imports_without_the_sdk(self):
        assert otel.is_configured() is False

    def test_configure_returns_false_when_instrumentation_is_missing(self, caplog):
        os.environ["OTEL_TRACES_EXPORTER"] = "console"
        with caplog.at_level(logging.WARNING), module_missing("opentelemetry.instrumentation.django"):
            assert otel.configure("svc") is False
        assert "netix-backend[otel]" in caplog.text
        assert otel.is_configured() is False


class TestInitSdk:
    def test_leaves_a_provider_someone_else_installed(self):
        from opentelemetry.sdk.trace import TracerProvider

        with mock.patch("opentelemetry.trace.get_tracer_provider", return_value=TracerProvider()):
            assert otel._init_sdk("console") is False

    def test_exporter_none_installs_nothing(self):
        with (
            mock.patch("opentelemetry.trace.get_tracer_provider", return_value=object()),
            mock.patch("opentelemetry.trace.set_tracer_provider") as set_provider,
        ):
            assert otel._init_sdk("none") is False
            set_provider.assert_not_called()

    def test_console_exporter(self):
        with (
            mock.patch("opentelemetry.trace.get_tracer_provider", return_value=object()),
            mock.patch("opentelemetry.trace.set_tracer_provider") as set_provider,
            mock.patch("opentelemetry.sdk.trace.export.ConsoleSpanExporter") as exporter,
            mock.patch("opentelemetry.sdk.trace.export.BatchSpanProcessor") as processor,
        ):
            assert otel._init_sdk("console") is True
            exporter.assert_called_once_with()
            processor.assert_called_once_with(exporter.return_value)
            set_provider.assert_called_once()

    def test_otlp_exporter(self):
        with (
            mock.patch("opentelemetry.trace.get_tracer_provider", return_value=object()),
            mock.patch("opentelemetry.trace.set_tracer_provider"),
            mock.patch("opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter") as exporter,
            mock.patch("opentelemetry.sdk.trace.export.BatchSpanProcessor"),
        ):
            assert otel._init_sdk("otlp") is True
            exporter.assert_called_once_with()


class TestSamplerKwargs:
    def test_uses_the_env_sampler(self):
        os.environ["OTEL_TRACES_SAMPLER"] = "always_off"
        kwargs = otel._sampler_kwargs()
        assert type(kwargs["sampler"]).__name__ == "StaticSampler"

    def test_falls_back_when_the_private_sdk_symbol_disappears(self, monkeypatch, caplog):
        """_get_from_env_or_default is private SDK API; the library must not break when a 1.x bump drops it."""
        import opentelemetry.sdk.trace.sampling as sampling

        monkeypatch.delattr(sampling, "_get_from_env_or_default")
        with caplog.at_level(logging.WARNING):
            assert otel._sampler_kwargs() == {}
        assert "_get_from_env_or_default" in caplog.text


class TestInstrumentOptional:
    def test_instruments_when_available(self):
        module = ModuleType("netix_fake_instrumentation")
        module.FakeInstrumentor = mock.Mock()
        with mock.patch.dict(sys.modules, {"netix_fake_instrumentation": module}):
            otel._instrument_optional("netix_fake_instrumentation", "FakeInstrumentor")
        module.FakeInstrumentor.return_value.instrument.assert_called_once_with()

    def test_swallows_import_error(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="netix_backend.observability.otel"):
            otel._instrument_optional("opentelemetry.instrumentation.does_not_exist", "Nope")
        assert "unavailable" in caplog.text

    def test_a_raising_instrumentor_does_not_kill_boot(self, caplog):
        module = ModuleType("netix_broken_instrumentation")
        module.BrokenInstrumentor = mock.Mock(side_effect=RuntimeError("instrument failed"))
        with (
            mock.patch.dict(sys.modules, {"netix_broken_instrumentation": module}),
            caplog.at_level(logging.ERROR),
        ):
            otel._instrument_optional("netix_broken_instrumentation", "BrokenInstrumentor")
        assert "failed to instrument" in caplog.text
