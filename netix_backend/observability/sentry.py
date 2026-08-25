"""One-call ``sentry_sdk.init`` for NETIX services; the SDK is imported inside the function, never at module scope."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from netix_backend.env import OMIT, _Omit

__all__ = ("OMIT", "configure_sentry")


def _integrations(
    integrations: Sequence[Any] | None,
    django_integration: bool,
    logging_event_level: str | None | _Omit,
) -> list[Any]:
    """Resolve the integration list, importing each SDK integration only where it is actually wanted."""
    resolved: list[Any] = list(integrations) if integrations is not None else []
    if integrations is None and django_integration:
        from sentry_sdk.integrations.django import DjangoIntegration  # type: ignore[import-not-found]

        resolved.append(DjangoIntegration())
    # Not OMIT-vs-None: OMIT drops the integration, None passes event_level=None, which backend-template needs.
    if not isinstance(logging_event_level, _Omit):
        from sentry_sdk.integrations.logging import LoggingIntegration  # type: ignore[import-not-found]

        resolved.append(LoggingIntegration(event_level=logging_event_level))
    return resolved


def configure_sentry(
    *,
    enabled: bool,
    dsn: str,
    # Required with no default: the helper must never invent an environment, so a hardcoded "main" stays visible
    # as environment="main" at the call site and an env read stays an env read.
    environment: str,
    django_integration: bool = True,
    logging_event_level: str | None | _Omit = OMIT,
    integrations: Sequence[Any] | None = None,
    disabled_integrations: Sequence[Any] | _Omit = OMIT,
    traces_sample_rate: int | float | _Omit = 0,
    before_send: Callable[..., Any] | _Omit = OMIT,
    ignore_loggers: Iterable[str] = (),
    tags: Mapping[str, str] | None = None,
    **init_kwargs: Any,
) -> bool:
    """Init Sentry and apply the ``ignore_logger`` tail; returns whether init actually ran."""
    if not enabled:
        return False
    # Imported here, not at module scope: every service imports the SDK inside its own `if SENTRY_ENABLED` today,
    # so a module-level import would newly load sentry_sdk in every disabled and test process.
    import sentry_sdk  # type: ignore[import-not-found]

    # No ignore here: sentry-sdk is not a dependency, and mypy reports each missing module once, at its first import.
    from sentry_sdk.integrations.logging import ignore_logger

    kwargs: dict[str, Any] = {
        "dsn": dsn,
        "environment": environment,
        "integrations": _integrations(integrations, django_integration, logging_event_level),
    }
    # OMIT is not 0: the SDK treats an absent traces_sample_rate as tracing-off and 0 as tracing-on at 0% sampling.
    if not isinstance(traces_sample_rate, _Omit):
        kwargs["traces_sample_rate"] = traces_sample_rate
    if not isinstance(before_send, _Omit):
        kwargs["before_send"] = before_send
    if not isinstance(disabled_integrations, _Omit):
        kwargs["disabled_integrations"] = list(disabled_integrations)
    kwargs.update(init_kwargs)
    # An empty dsn is deliberately not short-circuited: simulator-service inits with one and still needs the tail.
    sentry_sdk.init(**kwargs)
    for tag_name, tag_value in (tags or {}).items():
        sentry_sdk.set_tag(tag_name, tag_value)
    for logger_name in ignore_loggers:
        ignore_logger(logger_name)
    return True
