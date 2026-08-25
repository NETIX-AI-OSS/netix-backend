"""Observability helpers: the OTel bootstrap, the console logging config, and Sentry noise filters."""

from netix_backend.observability.logging import (
    CONSOLE_FORMAT,
    DEFAULT_LOGGERS,
    LOG_CONTEXT_KEY,
    TRACE_ID_DEFAULTS,
    TRACE_ID_FIELDS,
    ContextFormatter,
    log_context,
    logging_config,
)
from netix_backend.observability.otel import configure, is_configured, reset_for_tests
from netix_backend.observability.sentry_filters import (
    IGNORED_TELEMETRY_LOGGERS,
    chain,
    drop_cancelled_errors,
    drop_client_errors,
    drop_matching_signatures,
    event_text,
    event_text_candidates,
    exception_chain_text,
    fingerprint_matching_signatures,
    group_log_events_by_template,
    hint_exception,
)

__all__ = (
    "CONSOLE_FORMAT",
    "DEFAULT_LOGGERS",
    "IGNORED_TELEMETRY_LOGGERS",
    "LOG_CONTEXT_KEY",
    "TRACE_ID_DEFAULTS",
    "TRACE_ID_FIELDS",
    "ContextFormatter",
    "chain",
    "configure",
    "drop_cancelled_errors",
    "drop_client_errors",
    "drop_matching_signatures",
    "event_text",
    "event_text_candidates",
    "exception_chain_text",
    "fingerprint_matching_signatures",
    "group_log_events_by_template",
    "hint_exception",
    "is_configured",
    "log_context",
    "logging_config",
    "reset_for_tests",
)
