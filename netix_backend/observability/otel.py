"""OpenTelemetry bootstrap — vendored; call configure() from both asgi.py and manage.py, post-fork."""

from __future__ import annotations

import logging
import os
from typing import Any

from netix_backend.observability.logging import TRACE_ID_FIELDS

logger = logging.getLogger(__name__)

_configured = False


def is_configured() -> bool:
    """True once configure() has installed the instrumentors in this process."""
    return _configured


def reset_for_tests() -> None:
    """Clear the process latch so a test can call configure() again; never call this from service code."""
    global _configured
    _configured = False


def _enabled() -> bool:
    """Enabled per the OTel spec default; OTEL_SDK_DISABLED=true is the explicit opt-out."""
    return os.environ.get("OTEL_SDK_DISABLED", "false").strip().lower() not in ("true", "1", "yes")


def _truthy(name: str, default: str = "TRUE") -> bool:
    """Read a ConfigMap flag that is on unless explicitly turned off."""
    return os.environ.get(name, default).strip().upper() in ("TRUE", "1", "YES")


def configure(service_name: str | None = None) -> bool:
    """Install the SDK + instrumentors; idempotent, env-driven, returns True if this call did the install."""
    global _configured

    if _configured:
        return False
    if not _enabled():
        return False

    exporter = os.environ.get("OTEL_TRACES_EXPORTER", "otlp").strip().lower()
    # Gate on reality, not a bespoke flag: a cluster without a collector endpoint no-ops loudly.
    if exporter == "otlp" and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        logger.warning("otel: no OTEL_EXPORTER_OTLP_ENDPOINT configured; tracing stays off")
        return False

    # ConfigMap's OTEL_SERVICE_NAME wins over the code argument so a rename needs no code change.
    if service_name and not os.environ.get("OTEL_SERVICE_NAME"):
        os.environ["OTEL_SERVICE_NAME"] = service_name

    try:
        from opentelemetry.instrumentation.django import DjangoInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
    except ImportError:
        logger.warning("otel: instrumentation missing (install netix-backend[otel]); tracing stays off")
        return False

    # Load-bearing: without opentelemetry-instrumentation-asgi the Django middleware silently skips every ASGI request.
    try:
        import opentelemetry.instrumentation.asgi  # noqa: F401
    except ImportError:
        logger.error("otel: opentelemetry-instrumentation-asgi missing — ASGI (uvicorn) requests will NOT be traced")

    # Set BEFORE instrumenting so a raising instrumentor cannot cause a double-instrument on retry.
    _configured = True

    # Without a real TracerProvider every span is a non-recording no-op while the app looks instrumented.
    _init_sdk(exporter)

    DjangoInstrumentor().instrument()

    # DB spans are off only where a service opts out (per-statement spans dominate ingest-path trace volume).
    if _truthy("NETIX_OTEL_DB"):
        _instrument_optional("opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor")

    _instrument_optional("opentelemetry.instrumentation.redis", "RedisInstrumentor")
    _instrument_optional("opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor")

    correlate = _truthy("OTEL_PYTHON_LOG_CORRELATION")
    # Both kwargs are load-bearing: set_logging_format=False alone also disables otelTraceID/otelSpanID injection.
    LoggingInstrumentor().instrument(set_logging_format=False, inject_trace_context=correlate)

    logger.info(
        "otel: tracing enabled for service=%s, log correlation fields=%s",
        os.environ.get("OTEL_SERVICE_NAME", "<unset>"),
        ", ".join(TRACE_ID_FIELDS) if correlate else "off",
    )
    return True


def _sampler_kwargs() -> dict[str, Any]:
    """Honour OTEL_TRACES_SAMPLER via a private SDK symbol; drop the kwarg if a future SDK removes it."""
    try:
        from opentelemetry.sdk.trace.sampling import _get_from_env_or_default
    except ImportError:
        logger.warning("otel: sampling._get_from_env_or_default is gone; falling back to the SDK's own default")
        return {}
    return {"sampler": _get_from_env_or_default()}


def _init_sdk(exporter: str) -> bool:
    """Install a TracerProvider from OTEL_* env (resource, sampler, exporter); True if this call installed it."""
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

    # Respect a provider someone else installed (opentelemetry-instrument, a test harness) — never replace it.
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        logger.debug("otel: SDK TracerProvider already installed, leaving it")
        return False

    if exporter == "none":
        logger.info("otel: OTEL_TRACES_EXPORTER=none, not installing an exporter")
        return False

    provider = TracerProvider(resource=Resource.create(), **_sampler_kwargs())

    span_exporter: SpanExporter
    if exporter == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        span_exporter = ConsoleSpanExporter()
    else:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        span_exporter = OTLPSpanExporter()

    # BatchSpanProcessor's export thread must be created post-fork, which is why configure() runs in the worker.
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)
    return True


def _instrument_optional(module_path: str, class_name: str) -> None:
    """Instrument a library this service may not ship; degrade quietly instead of killing boot."""
    try:
        module = __import__(module_path, fromlist=[class_name])
        getattr(module, class_name)().instrument()
    except ImportError:
        logger.debug("otel: %s unavailable, skipping", module_path)
    except Exception:
        logger.exception("otel: %s failed to instrument, continuing", module_path)


__all__ = ("configure", "is_configured", "reset_for_tests")
