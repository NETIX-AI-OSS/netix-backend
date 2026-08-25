"""Console logging shared by the fleet: the OTel-correlated formatter plus opt-in structured context."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

# The record attributes opentelemetry-instrumentation-logging injects once inject_trace_context is on.
TRACE_ID_FIELDS: tuple[str, ...] = ("otelTraceID", "otelSpanID")

# Load-bearing: without these the formatter raises KeyError on every record whenever tracing is off.
TRACE_ID_DEFAULTS: Mapping[str, str] = MappingProxyType(dict.fromkeys(TRACE_ID_FIELDS, "0"))

# trace_id/span_id must be in the format string or Loki cannot join a log line to its Tempo trace.
CONSOLE_FORMAT = "%(levelname)s %(asctime)s %(name)s trace_id=%(otelTraceID)s span_id=%(otelSpanID)s %(message)s"

# Namespaced key ContextFormatter reads; avoids colliding with LogRecord attrs.
LOG_CONTEXT_KEY = "context"

# What the template family configures today; pass loggers= to replace it wholesale.
DEFAULT_LOGGERS: Mapping[str, str] = MappingProxyType({"django": "INFO", "django.template": "INFO"})


def log_context(**fields: Any) -> dict[str, Any]:
    """Build the ``extra=`` payload ``ContextFormatter`` renders, e.g. ``log_context(config_id=config.pk)``."""
    return {LOG_CONTEXT_KEY: fields}


class ContextFormatter(logging.Formatter):
    """Append an opted-in record's context to the console line as ``[key=value ...]``."""

    def format(self, record: logging.LogRecord) -> str:
        """Render the record, then splice its context onto the first line."""
        formatted = super().format(record)
        context = getattr(record, LOG_CONTEXT_KEY, None)
        # Mapping check, not truthiness: unmapped values aren't ours to render.
        if not isinstance(context, Mapping) or not context:
            return formatted
        rendered = " ".join(f"{key}={value}" for key, value in context.items())
        # Splice onto the first line; exc_info's traceback is already appended after it.
        message_line, separator, remainder = formatted.partition("\n")
        return f"{message_line} [{rendered}]{separator}{remainder}"


def _formatter_class_path(formatter_class: str | type[logging.Formatter]) -> str:
    """dictConfig wants a dotted path; accept the class itself so a caller cannot mistype it."""
    if isinstance(formatter_class, str):
        return formatter_class
    return f"{formatter_class.__module__}.{formatter_class.__qualname__}"


def _logger_entry(spec: str | Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a ``loggers=`` value: a bare level, or a dictConfig entry merged over the console defaults."""
    entry: dict[str, Any] = {"handlers": ["console"], "propagate": False}
    if isinstance(spec, str):
        entry["level"] = spec
    else:
        entry.update(spec)
    return entry


def logging_config(
    *,
    root_level: str = "WARNING",
    loggers: Mapping[str, str | Mapping[str, Any]] | None = None,
    formatter_class: str | type[logging.Formatter] | None = None,
    console_format: str = CONSOLE_FORMAT,
    disable_existing_loggers: bool = False,
) -> dict[str, Any]:
    """Build the fleet's LOGGING dictConfig; ``root_level`` is taken verbatim so ``os.getenv(...)`` still works."""
    formatter: dict[str, Any] = {"format": console_format, "defaults": dict(TRACE_ID_DEFAULTS)}
    if formatter_class is not None:
        formatter["class"] = _formatter_class_path(formatter_class)
    entries = DEFAULT_LOGGERS if loggers is None else loggers
    return {
        "version": 1,
        "disable_existing_loggers": disable_existing_loggers,
        "formatters": {"console": formatter},
        "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "console"}},
        "root": {"handlers": ["console"], "level": root_level},
        "loggers": {name: _logger_entry(spec) for name, spec in entries.items()},
    }


__all__ = (
    "CONSOLE_FORMAT",
    "DEFAULT_LOGGERS",
    "LOG_CONTEXT_KEY",
    "TRACE_ID_DEFAULTS",
    "TRACE_ID_FIELDS",
    "ContextFormatter",
    "log_context",
    "logging_config",
)
