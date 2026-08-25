"""configure_sentry against a simulated sentry_sdk; the library must not depend on the SDK, not even for tests."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

from netix_backend.env import OMIT
from netix_backend.observability.sentry import configure_sentry

DSN = "https://key@sentry.invalid/1"


class FakeDjangoIntegration:
    """Stands in for sentry_sdk.integrations.django.DjangoIntegration; only its identity is under test."""


class FakeLoggingIntegration:
    """Stands in for LoggingIntegration, recording the event_level the helper chose to pass."""

    def __init__(self, event_level: Any = "ERROR") -> None:
        self.event_level = event_level


@dataclass
class SentryCalls:
    """Everything the fake SDK saw, in call order."""

    init: list[dict[str, Any]] = field(default_factory=list)
    tags: list[tuple[str, str]] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    def record_init(self, **kwargs: Any) -> None:
        self.init.append(kwargs)

    def record_tag(self, name: str, value: str) -> None:
        self.tags.append((name, value))

    def record_ignored(self, name: str) -> None:
        self.ignored.append(name)

    @property
    def kwargs(self) -> dict[str, Any]:
        """The single sentry_sdk.init call this test made."""
        assert len(self.init) == 1
        return self.init[0]


def fake_module(name: str, **attributes: Any) -> ModuleType:
    """A bare module object carrying *attributes*, which is all `from x import y` needs to succeed."""
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@pytest.fixture
def sentry(monkeypatch: pytest.MonkeyPatch) -> SentryCalls:
    """Inject a fake sentry_sdk into sys.modules, so the function-local imports resolve without the real SDK."""
    calls = SentryCalls()
    modules = {
        "sentry_sdk": fake_module("sentry_sdk", init=calls.record_init, set_tag=calls.record_tag),
        "sentry_sdk.integrations": fake_module("sentry_sdk.integrations"),
        "sentry_sdk.integrations.logging": fake_module(
            "sentry_sdk.integrations.logging",
            ignore_logger=calls.record_ignored,
            LoggingIntegration=FakeLoggingIntegration,
        ),
        "sentry_sdk.integrations.django": fake_module(
            "sentry_sdk.integrations.django", DjangoIntegration=FakeDjangoIntegration
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return calls


def configure(**kwargs: Any) -> bool:
    """Call the helper with the three arguments every call site supplies, so each test states only its own knob."""
    return configure_sentry(enabled=True, dsn=DSN, environment="main", **kwargs)


class TestDisabled:
    def test_returns_false_without_touching_the_sdk(self) -> None:
        # Invariant 5: a disabled service must not import sentry_sdk at all, so this test runs without the fake.
        assert configure_sentry(enabled=False, dsn=DSN, environment="main") is False
        assert "sentry_sdk" not in sys.modules

    def test_the_library_does_not_depend_on_the_sdk(self) -> None:
        # A dependency on sentry-sdk would make every consumer install it; the tests simulate it instead.
        assert importlib.util.find_spec("sentry_sdk") is None


class TestInitKwargs:
    def test_returns_true_and_passes_dsn_and_environment(self, sentry: SentryCalls) -> None:
        assert configure() is True
        assert sentry.kwargs["dsn"] == DSN
        assert sentry.kwargs["environment"] == "main"

    def test_environment_has_no_default(self) -> None:
        # Invariant 1: the helper must never invent an environment.
        with pytest.raises(TypeError, match="environment"):
            configure_sentry(enabled=True, dsn=DSN)

    def test_an_empty_dsn_is_passed_through(self, sentry: SentryCalls) -> None:
        # H10: simulator-service inits with an empty DSN on purpose and still needs the ignore_logger tail.
        assert configure_sentry(enabled=True, dsn="", environment="main", ignore_loggers=("noisy",)) is True
        assert sentry.kwargs["dsn"] == ""
        assert sentry.ignored == ["noisy"]

    def test_unknown_kwargs_reach_init(self, sentry: SentryCalls) -> None:
        configure(send_default_pii=True)
        assert sentry.kwargs["send_default_pii"] is True

    def test_traces_sample_rate_defaults_to_zero(self, sentry: SentryCalls) -> None:
        configure()
        assert sentry.kwargs["traces_sample_rate"] == 0

    def test_traces_sample_rate_omit_drops_the_kwarg(self, sentry: SentryCalls) -> None:
        # H7 / invariant 3: user-management omits it, and an absent rate is not the same as 0 to the SDK.
        configure(traces_sample_rate=OMIT)
        assert "traces_sample_rate" not in sentry.kwargs

    def test_traces_sample_rate_can_be_a_real_rate(self, sentry: SentryCalls) -> None:
        configure(traces_sample_rate=0.25)
        assert sentry.kwargs["traces_sample_rate"] == 0.25

    def test_before_send_is_absent_by_default(self, sentry: SentryCalls) -> None:
        # Invariant 4: backend-template passes no before_send at all.
        configure()
        assert "before_send" not in sentry.kwargs

    def test_before_send_is_passed_verbatim(self, sentry: SentryCalls) -> None:
        def before_send(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
            return event

        configure(before_send=before_send)
        assert sentry.kwargs["before_send"] is before_send


class TestIntegrations:
    def test_django_integration_is_the_default(self, sentry: SentryCalls) -> None:
        configure()
        (integration,) = sentry.kwargs["integrations"]
        assert isinstance(integration, FakeDjangoIntegration)

    def test_the_django_integration_can_be_declined(self, sentry: SentryCalls) -> None:
        configure(django_integration=False)
        assert sentry.kwargs["integrations"] == []

    def test_an_explicit_list_replaces_the_default(self, sentry: SentryCalls) -> None:
        marker = object()
        configure(integrations=[marker])
        assert sentry.kwargs["integrations"] == [marker]

    def test_logging_integration_is_absent_by_default(self, sentry: SentryCalls) -> None:
        configure()
        assert len(sentry.kwargs["integrations"]) == 1

    def test_logging_event_level_none_is_not_omit(self, sentry: SentryCalls) -> None:
        # backend-template is the only repo with LoggingIntegration(event_level=None); OMIT means "no integration".
        configure(logging_event_level=None)
        django_integration, logging_integration = sentry.kwargs["integrations"]
        assert isinstance(django_integration, FakeDjangoIntegration)
        assert isinstance(logging_integration, FakeLoggingIntegration)
        assert logging_integration.event_level is None

    def test_logging_event_level_can_be_a_level_name(self, sentry: SentryCalls) -> None:
        configure(logging_event_level="ERROR", integrations=[])
        (logging_integration,) = sentry.kwargs["integrations"]
        assert logging_integration.event_level == "ERROR"

    def test_disabled_integrations_are_absent_by_default(self, sentry: SentryCalls) -> None:
        configure()
        assert "disabled_integrations" not in sentry.kwargs

    def test_disabled_integrations_are_copied_into_a_list(self, sentry: SentryCalls) -> None:
        # ml-engine instantiates OpenAIIntegration() at the call site, so the SDK import stays inside its own guard.
        marker = object()
        configure(disabled_integrations=(marker,))
        assert sentry.kwargs["disabled_integrations"] == [marker]


class TestTail:
    def test_no_tags_are_set_by_default(self, sentry: SentryCalls) -> None:
        configure()
        assert sentry.tags == []

    def test_tags_replace_the_post_init_set_tag_calls(self, sentry: SentryCalls) -> None:
        configure(tags={"mode": "django"})
        assert sentry.tags == [("mode", "django")]

    def test_no_loggers_are_ignored_by_default(self, sentry: SentryCalls) -> None:
        configure()
        assert sentry.ignored == []

    def test_ignore_loggers_keeps_the_call_order(self, sentry: SentryCalls) -> None:
        configure(ignore_loggers=("django_redis.cache", "opentelemetry.context"))
        assert sentry.ignored == ["django_redis.cache", "opentelemetry.context"]
