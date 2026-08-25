"""Sentry ``before_send`` noise filters as pure functions over event dicts, so sentry_sdk is never imported here."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

# Mirrors sentry_sdk.types.Event/Hint without importing the SDK; before_send sees plain dicts either way.
Event = dict[str, Any]
Hint = Mapping[str, Any]
EventFilter = Callable[[Event, Hint | None], Event | None]

# Muted at source by sentry_sdk's ignore_logger, which matches exactly and never by prefix.
IGNORED_TELEMETRY_LOGGERS: tuple[str, ...] = (
    "opentelemetry.context",  # "Failed to detach context" -- OTel's own logger.exception, handled=true
    "opentelemetry.exporter.otlp.proto.grpc.exporter",  # Tempo OTLP export retries / DEADLINE_EXCEEDED
    "opentelemetry.sdk.trace.export",  # BatchSpanProcessor export failures
)


def hint_exception(hint: Hint | None) -> BaseException | None:
    """Best-effort exception object from a Sentry ``before_send`` hint."""
    if not isinstance(hint, Mapping) or not hint:
        return None
    exc_info = hint.get("exc_info")
    if isinstance(exc_info, tuple) and len(exc_info) >= 2:
        exc = exc_info[1]
        return exc if isinstance(exc, BaseException) else None
    original = hint.get("originalException")
    return original if isinstance(original, BaseException) else None


def exception_chain_text(exc: BaseException | None) -> list[str]:
    """``["Type: msg", ...]`` for *exc* and its ``__cause__``/``__context__`` chain, guarded against cycles."""
    out: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        out.append(f"{type(current).__name__}: {current}")
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return out


def _exception_values(event: Event) -> list[Any]:
    """The serialized exception entries Sentry ships on an event, defensively unwrapped."""
    exception = event.get("exception") if isinstance(event, Mapping) else None
    values = exception.get("values") if isinstance(exception, Mapping) else None
    return values if isinstance(values, list) else []


def event_text_candidates(event: Event, hint: Hint | None = None) -> list[str]:
    """Every place Sentry may carry the message, as a list of strings."""
    candidates = exception_chain_text(hint_exception(hint))
    if not isinstance(event, Mapping):
        return candidates
    # Serialized exception values — logging-integration events carry no exc_info.
    for exc_data in _exception_values(event):
        if not isinstance(exc_data, Mapping):
            continue
        exc_type, exc_val = exc_data.get("type"), exc_data.get("value")
        if exc_val:
            candidates.append(f"{exc_type}: {exc_val}" if exc_type else str(exc_val))
    # Log entry — both the "%s"-style template and the formatted string.
    logentry = event.get("logentry") or {}
    if isinstance(logentry, Mapping):
        candidates += [str(logentry[key]) for key in ("message", "formatted") if logentry.get(key)]
    # Event message — a plain string or a {message, formatted} dict.
    event_msg = event.get("message")
    if isinstance(event_msg, Mapping):
        candidates += [str(event_msg.get("message") or ""), str(event_msg.get("formatted") or "")]
    elif event_msg:
        candidates.append(str(event_msg))
    return candidates


def event_text(event: Event, hint: Hint | None = None) -> str:
    """The candidates joined into one blob, which is what every signature rule matches against."""
    return "\n".join(event_text_candidates(event, hint))


def drop_cancelled_errors(event: Event, hint: Hint | None = None) -> Event | None:
    """Drop ASGI request cancellations, which are expected when clients disconnect or probes time out."""
    if isinstance(hint_exception(hint), asyncio.CancelledError):
        return None
    # Scan every serialized value, not just the last: a cancellation is rarely the terminal frame.
    if any(isinstance(value, Mapping) and value.get("type") == "CancelledError" for value in _exception_values(event)):
        return None
    return event


def drop_client_errors(
    *exception_classes: type[BaseException],
    status_range: tuple[int, int] = (400, 500),
) -> EventFilter:
    """Build a filter dropping caller-fault API exceptions; classes are passed in so DRF stays out of this module."""
    classes: tuple[type[BaseException], ...] = exception_classes or (BaseException,)
    low, high = status_range

    def _drop_client_errors(event: Event, hint: Hint | None = None) -> Event | None:
        exc = hint_exception(hint)
        if not isinstance(exc, classes):
            return event
        status_code = getattr(exc, "status_code", None)
        # isinstance(int), not int(): a raising before_send would drop every event.
        if isinstance(status_code, int) and low <= status_code < high:
            return None
        return event

    return _drop_client_errors


def drop_matching_signatures(signatures: Sequence[str]) -> EventFilter:
    """Build a filter dropping events whose text carries any of *signatures*; keep the list service-local."""

    def _drop_matching_signatures(event: Event, hint: Hint | None = None) -> Event | None:
        if any(signature in event_text(event, hint) for signature in signatures):
            return None
        return event

    return _drop_matching_signatures


def fingerprint_matching_signatures(signatures: Sequence[str], fingerprint: Sequence[str]) -> EventFilter:
    """Build a filter pinning events matching *signatures* to one issue; an already-set fingerprint always wins."""

    def _fingerprint_matching_signatures(event: Event, hint: Hint | None = None) -> Event | None:
        if not isinstance(event, Mapping) or event.get("fingerprint"):
            return event
        if any(signature in event_text(event, hint) for signature in signatures):
            event["fingerprint"] = list(fingerprint)
        return event

    return _fingerprint_matching_signatures


def group_log_events_by_template(event: Event, hint: Hint | None = None) -> Event:
    """Fingerprint on ``[logger, template, exc type]`` so a per-entity id in the message cannot fan out issues."""
    record = hint.get("log_record") if isinstance(hint, Mapping) else None
    message_template = getattr(record, "msg", None)
    # Only LoggingIntegration records carry a template; nothing else can be regrouped this way.
    if not isinstance(message_template, str):
        return event
    # An explicitly set fingerprint (ours or the SDK's) always wins -- never override a deliberate choice.
    if not isinstance(event, Mapping) or event.get("fingerprint"):
        return event
    fingerprint = [getattr(record, "name", ""), message_template]
    exc_info = getattr(record, "exc_info", None)
    # Append the exception type so a shared catch-all message does not merge genuinely different faults.
    if exc_info and exc_info[0] is not None:
        fingerprint.append(exc_info[0].__name__)
    event["fingerprint"] = fingerprint
    return event


def chain(*filters: EventFilter) -> EventFilter:
    """Compose ``before_send`` filters; one that raises is skipped, because a raising hook drops every event."""

    def _chained(event: Event, hint: Hint | None = None) -> Event | None:
        current = event
        for event_filter in filters:
            try:
                result = event_filter(current, hint)
            except Exception:
                logger.exception("sentry before_send filter %r raised; keeping the event unchanged", event_filter)
                continue
            if result is None:
                return None
            current = result
        return current

    return _chained


__all__ = (
    "IGNORED_TELEMETRY_LOGGERS",
    "Event",
    "EventFilter",
    "Hint",
    "chain",
    "drop_cancelled_errors",
    "drop_client_errors",
    "drop_matching_signatures",
    "event_text",
    "event_text_candidates",
    "exception_chain_text",
    "fingerprint_matching_signatures",
    "group_log_events_by_template",
    "hint_exception",
)
