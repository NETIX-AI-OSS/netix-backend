"""Tests for the console logging config: the trace-id defaults are what keep logging alive when tracing is off."""

from __future__ import annotations

import logging
import logging.config
import sys

import pytest

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


def make_record(message: str = "hello", **extra: object) -> logging.LogRecord:
    """A bare LogRecord with optional record attributes, standing in for a real emit."""
    record = logging.LogRecord("svc", logging.INFO, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestTraceCorrelationInvariant:
    """The one thing the 17 copies exist to preserve: a trace id on every line, and no KeyError without one."""

    def test_format_string_references_every_injected_field(self):
        for field in TRACE_ID_FIELDS:
            assert f"%({field})s" in CONSOLE_FORMAT

    def test_defaults_cover_every_injected_field(self):
        assert set(TRACE_ID_DEFAULTS) == set(TRACE_ID_FIELDS)

    def test_defaults_keep_formatting_alive_when_tracing_is_off(self):
        formatter = logging.Formatter(CONSOLE_FORMAT, defaults=dict(TRACE_ID_DEFAULTS))
        assert "trace_id=0 span_id=0" in formatter.format(make_record())

    def test_without_defaults_the_same_record_raises(self):
        """Proves the defaults dict is load-bearing rather than decorative."""
        formatter = logging.Formatter(CONSOLE_FORMAT)
        with pytest.raises(ValueError):
            formatter.format(make_record())


class TestContextFormatter:
    def test_plain_record_is_untouched(self):
        formatter = ContextFormatter("%(message)s")
        assert formatter.format(make_record()) == "hello"

    def test_context_is_appended_to_the_first_line(self):
        formatter = ContextFormatter("%(message)s")
        record = make_record(**log_context(config_id=7, org=3))
        assert formatter.format(record) == "hello [config_id=7 org=3]"

    def test_context_lands_before_the_traceback(self):
        formatter = ContextFormatter("%(message)s")
        try:
            raise ValueError("boom")
        except ValueError:
            record = make_record(**log_context(job="x"))
            record.exc_info = sys.exc_info()
        first_line, _, remainder = formatter.format(record).partition("\n")
        assert first_line == "hello [job=x]"
        assert "ValueError: boom" in remainder

    def test_empty_context_renders_nothing(self):
        formatter = ContextFormatter("%(message)s")
        assert formatter.format(make_record(**log_context())) == "hello"

    def test_non_mapping_under_the_key_is_not_ours_to_render(self):
        formatter = ContextFormatter("%(message)s")
        assert formatter.format(make_record(context="a string")) == "hello"

    def test_log_context_namespaces_its_payload(self):
        assert log_context(a=1) == {LOG_CONTEXT_KEY: {"a": 1}}


class TestLoggingConfig:
    def test_defaults_match_the_template_shape(self):
        config = logging_config()
        assert config["version"] == 1
        assert config["disable_existing_loggers"] is False
        assert config["root"] == {"handlers": ["console"], "level": "WARNING"}
        assert config["handlers"] == {"console": {"class": "logging.StreamHandler", "formatter": "console"}}
        assert config["formatters"]["console"]["format"] == CONSOLE_FORMAT
        assert config["formatters"]["console"]["defaults"] == dict(TRACE_ID_DEFAULTS)
        # No formatter class by default: adopting the factory must not silently change the formatter for 14 repos.
        assert "class" not in config["formatters"]["console"]

    def test_default_loggers_are_the_template_pair(self):
        config = logging_config()
        assert set(config["loggers"]) == set(DEFAULT_LOGGERS)
        assert config["loggers"]["django"] == {"handlers": ["console"], "propagate": False, "level": "INFO"}

    def test_root_level_is_taken_verbatim(self):
        """stormbreaker/data-service pass os.getenv("DJANGO_LOG_LEVEL", "WARN") — "WARN", not "WARNING"."""
        assert logging_config(root_level="WARN")["root"]["level"] == "WARN"

    def test_loggers_accepts_a_bare_level(self):
        config = logging_config(loggers={"tag": "INFO"})
        assert config["loggers"] == {"tag": {"handlers": ["console"], "propagate": False, "level": "INFO"}}

    def test_loggers_accepts_a_full_entry_with_propagate(self):
        config = logging_config(loggers={"django.db.backends": {"level": "DEBUG", "propagate": True}})
        assert config["loggers"]["django.db.backends"] == {
            "handlers": ["console"],
            "propagate": True,
            "level": "DEBUG",
        }

    def test_empty_loggers_mapping_is_honoured(self):
        assert logging_config(loggers={})["loggers"] == {}

    def test_formatter_class_accepts_a_class(self):
        config = logging_config(formatter_class=ContextFormatter)
        assert config["formatters"]["console"]["class"] == "netix_backend.observability.logging.ContextFormatter"

    def test_formatter_class_accepts_a_dotted_path(self):
        config = logging_config(formatter_class="app.log_formatters.ContextFormatter")
        assert config["formatters"]["console"]["class"] == "app.log_formatters.ContextFormatter"

    def test_console_format_is_overridable(self):
        config = logging_config(console_format="%(message)s")
        assert config["formatters"]["console"]["format"] == "%(message)s"

    def test_disable_existing_loggers_is_overridable(self):
        assert logging_config(disable_existing_loggers=True)["disable_existing_loggers"] is True

    def test_trace_defaults_are_copied_not_shared(self):
        config = logging_config()
        config["formatters"]["console"]["defaults"]["otelTraceID"] = "mutated"
        assert TRACE_ID_DEFAULTS["otelTraceID"] == "0"

    def test_the_generated_config_is_a_valid_dictconfig(self):
        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        try:
            logging.config.dictConfig(logging_config(loggers={"netix.test": "INFO"}))
            assert logging.getLogger("netix.test").level == logging.INFO
        finally:
            # Put the session's logging back: pytest's capture handlers live on the root logger.
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)
