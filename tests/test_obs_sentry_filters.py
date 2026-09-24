"""Tests for the Sentry before_send filters; every one of these guards an event the fleet must not lose."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from netix_backend.observability.sentry_filters import (
    ASGI_EXECUTOR_TEARDOWN_SIGNATURES,
    DB_CONNECT_SIGNATURES,
    DB_READONLY_SIGNATURES,
    DB_STATEMENT_TIMEOUT_SIGNATURES,
    IGNORED_TELEMETRY_LOGGERS,
    chain,
    drop_cancelled_errors,
    drop_client_errors,
    drop_matching_signatures,
    event_text,
    event_text_candidates,
    exception_chain_text,
    fingerprint_asgi_executor_teardown,
    fingerprint_db_infra_errors,
    fingerprint_db_statement_timeouts,
    fingerprint_matching_signatures,
    group_log_events_by_template,
    hint_exception,
)


class FakeApiException(Exception):
    """Stands in for DRF's APIException, which the library deliberately does not import."""

    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def serialized(*types_and_values) -> dict:
    """An event carrying Sentry's serialized exception values, the shape a logging-integration event has."""
    return {"exception": {"values": [{"type": t, "value": v} for t, v in types_and_values]}}


class TestHintException:
    def test_no_hint(self):
        assert hint_exception(None) is None

    def test_empty_hint(self):
        assert hint_exception({}) is None

    def test_non_mapping_hint(self):
        assert hint_exception("nope") is None

    def test_exc_info_tuple(self):
        exc = ValueError("boom")
        assert hint_exception({"exc_info": (ValueError, exc, None)}) is exc

    def test_exc_info_without_an_exception(self):
        assert hint_exception({"exc_info": (ValueError, "not an exception", None)}) is None

    def test_original_exception_fallback(self):
        exc = ValueError("boom")
        assert hint_exception({"originalException": exc}) is exc

    def test_original_exception_that_is_not_an_exception(self):
        assert hint_exception({"originalException": "boom"}) is None


class TestExceptionChainText:
    def test_none(self):
        assert exception_chain_text(None) == []

    def test_single(self):
        assert exception_chain_text(ValueError("boom")) == ["ValueError: boom"]

    def test_cause_chain(self):
        try:
            try:
                raise KeyError("inner")
            except KeyError as inner:
                raise ValueError("outer") from inner
        except ValueError as exc:
            assert exception_chain_text(exc) == ["ValueError: outer", "KeyError: 'inner'"]

    def test_context_chain(self):
        try:
            try:
                raise KeyError("inner")
            except KeyError:
                raise ValueError("outer")
        except ValueError as exc:
            assert exception_chain_text(exc) == ["ValueError: outer", "KeyError: 'inner'"]

    def test_self_referential_chain_terminates(self):
        """data-service's cut-down copy loops forever here; the seen-set is why this one does not."""
        exc = ValueError("loop")
        exc.__context__ = exc
        assert exception_chain_text(exc) == ["ValueError: loop"]


class TestEventTextCandidates:
    def test_non_mapping_event(self):
        assert event_text_candidates("not an event") == []

    def test_event_without_an_exception(self):
        assert event_text_candidates({}) == []

    def test_exception_that_is_not_a_mapping(self):
        assert event_text_candidates({"exception": "broken"}) == []

    def test_values_that_are_not_a_list(self):
        assert event_text_candidates({"exception": {"values": "broken"}}) == []

    def test_value_entry_that_is_not_a_mapping(self):
        assert event_text_candidates({"exception": {"values": ["broken"]}}) == []

    def test_typed_value(self):
        assert event_text_candidates(serialized(("CancelledError", "cancelled"))) == ["CancelledError: cancelled"]

    def test_untyped_value(self):
        assert event_text_candidates(serialized((None, "bare"))) == ["bare"]

    def test_empty_value_is_skipped(self):
        assert event_text_candidates(serialized(("ValueError", ""))) == []

    def test_hint_chain_comes_first(self):
        event = serialized(("ValueError", "serialized"))
        candidates = event_text_candidates(event, {"exc_info": (KeyError, KeyError("hinted"), None)})
        assert candidates == ["KeyError: 'hinted'", "ValueError: serialized"]

    def test_logentry_template_and_formatted(self):
        event = {"logentry": {"message": "job %s failed", "formatted": "job 42 failed"}}
        assert event_text_candidates(event) == ["job %s failed", "job 42 failed"]

    def test_logentry_that_is_not_a_mapping(self):
        assert event_text_candidates({"logentry": "broken"}) == []

    def test_message_string(self):
        assert event_text_candidates({"message": "plain"}) == ["plain"]

    def test_message_dict(self):
        event = {"message": {"message": "job %s failed", "formatted": "job 42 failed"}}
        assert event_text_candidates(event) == ["job %s failed", "job 42 failed"]

    def test_empty_message_is_skipped(self):
        assert event_text_candidates({"message": ""}) == []

    def test_event_text_joins_the_candidates(self):
        assert event_text(serialized(("A", "a"), ("B", "b"))) == "A: a\nB: b"


class TestDropCancelledErrors:
    def test_dropped_from_the_hint(self):
        hint = {"exc_info": (asyncio.CancelledError, asyncio.CancelledError(), None)}
        assert drop_cancelled_errors({}, hint) is None

    def test_dropped_from_the_original_exception(self):
        """The logging integration ships originalException, not exc_info — the weak copies miss this."""
        assert drop_cancelled_errors({}, {"originalException": asyncio.CancelledError()}) is None

    def test_dropped_when_not_the_terminal_frame(self):
        """redirect-service and visualization-backend inspect values[-1] only, so this event survives there."""
        event = serialized(("CancelledError", "cancelled"), ("RuntimeError", "teardown"))
        assert drop_cancelled_errors(event) is None

    def test_dropped_when_it_is_the_terminal_frame(self):
        assert drop_cancelled_errors(serialized(("CancelledError", "cancelled"))) is None

    def test_real_error_survives(self):
        event = serialized(("ValueError", "boom"))
        assert drop_cancelled_errors(event, {"exc_info": (ValueError, ValueError("boom"), None)}) is event

    def test_a_non_mapping_event_is_returned_rather_than_raising(self):
        assert drop_cancelled_errors("not an event") == "not an event"


class TestDropClientErrors:
    def test_drops_a_4xx_api_exception(self):
        drop = drop_client_errors(FakeApiException)
        hint = {"exc_info": (FakeApiException, FakeApiException(404), None)}
        assert drop({}, hint) is None

    def test_keeps_a_5xx(self):
        drop = drop_client_errors(FakeApiException)
        event = {}
        assert drop(event, {"originalException": FakeApiException(503)}) is event

    def test_keeps_an_unrelated_exception_class(self):
        drop = drop_client_errors(FakeApiException)
        event = {}
        assert drop(event, {"originalException": ValueError("boom")}) is event

    def test_keeps_an_event_with_no_exception(self):
        drop = drop_client_errors(FakeApiException)
        event = {}
        assert drop(event, None) is event

    def test_non_integer_status_code_is_not_coerced(self):
        """int() on a str status would raise, and a raising before_send drops every event."""
        drop = drop_client_errors(FakeApiException)
        event = {}
        assert drop(event, {"originalException": FakeApiException("404")}) is event

    def test_custom_status_range(self):
        drop = drop_client_errors(FakeApiException, status_range=(400, 600))
        assert drop({}, {"originalException": FakeApiException(503)}) is None

    def test_no_classes_matches_any_exception_carrying_a_status(self):
        drop = drop_client_errors()
        assert drop({}, {"originalException": FakeApiException(400)}) is None


class TestSignatureFilters:
    def test_drop_on_match(self):
        drop = drop_matching_signatures(("No master found for",))
        assert drop(serialized(("ConnectionError", "No master found for mymaster"))) is None

    def test_keep_without_match(self):
        drop = drop_matching_signatures(("No master found for",))
        event = serialized(("ValueError", "boom"))
        assert drop(event) is event

    def test_fingerprint_on_match(self):
        pin = fingerprint_matching_signatures(("in a read-only transaction",), ["db-infra", "postgres-read-only"])
        event = serialized(("OperationalError", "cannot execute in a read-only transaction"))
        assert pin(event)["fingerprint"] == ["db-infra", "postgres-read-only"]

    def test_no_fingerprint_without_match(self):
        pin = fingerprint_matching_signatures(("nope",), ["x"])
        assert "fingerprint" not in pin(serialized(("ValueError", "boom")))

    def test_an_existing_fingerprint_wins(self):
        pin = fingerprint_matching_signatures(("boom",), ["x"])
        event = {"fingerprint": ["mine"], **serialized(("ValueError", "boom"))}
        assert pin(event)["fingerprint"] == ["mine"]

    def test_non_mapping_event_is_returned_untouched(self):
        pin = fingerprint_matching_signatures(("boom",), ["x"])
        assert pin("not an event") == "not an event"


class TestGroupLogEventsByTemplate:
    def test_no_hint(self):
        event = {}
        assert group_log_events_by_template(event) is event

    def test_hint_without_a_log_record(self):
        event = {}
        assert group_log_events_by_template(event, {"exc_info": None}) is event

    def test_record_with_a_non_string_template(self):
        event = {}
        hint = {"log_record": SimpleNamespace(msg=object(), name="svc", exc_info=None)}
        assert group_log_events_by_template(event, hint) is event

    def test_non_mapping_event(self):
        hint = {"log_record": SimpleNamespace(msg="t", name="svc", exc_info=None)}
        assert group_log_events_by_template("not an event", hint) == "not an event"

    def test_an_existing_fingerprint_wins(self):
        hint = {"log_record": SimpleNamespace(msg="t", name="svc", exc_info=None)}
        assert group_log_events_by_template({"fingerprint": ["mine"]}, hint)["fingerprint"] == ["mine"]

    def test_fingerprints_on_the_template_not_the_rendered_text(self):
        record = logging.LogRecord("svc", logging.ERROR, __file__, 1, "job %s failed", (42,), None)
        assert group_log_events_by_template({}, {"log_record": record})["fingerprint"] == ["svc", "job %s failed"]

    def test_exception_type_is_appended(self):
        hint = {"log_record": SimpleNamespace(msg="t", name="svc", exc_info=(ValueError, ValueError("x"), None))}
        assert group_log_events_by_template({}, hint)["fingerprint"] == ["svc", "t", "ValueError"]

    def test_exc_info_without_a_type_is_ignored(self):
        hint = {"log_record": SimpleNamespace(msg="t", name="svc", exc_info=(None, None, None))}
        assert group_log_events_by_template({}, hint)["fingerprint"] == ["svc", "t"]


class TestChain:
    def test_empty_chain_is_a_passthrough(self):
        event = {}
        assert chain()(event) is event

    def test_filters_run_in_order(self):
        before_send = chain(
            fingerprint_matching_signatures(("boom",), ["first"]),
            fingerprint_matching_signatures(("boom",), ["second"]),
        )
        assert before_send(serialized(("ValueError", "boom")))["fingerprint"] == ["first"]

    def test_a_drop_short_circuits(self):
        calls = []

        def _tracker(event, hint=None):
            calls.append(event)
            return event

        before_send = chain(drop_cancelled_errors, _tracker)
        assert before_send(serialized(("CancelledError", "cancelled"))) is None
        assert calls == []

    def test_a_raising_filter_is_skipped_rather_than_dropping_the_event(self, caplog):
        """A before_send that raises drops every event, which is the hazard this combinator exists to remove."""

        def _broken(event, hint=None):
            raise RuntimeError("filter bug")

        event = {}
        with caplog.at_level(logging.ERROR):
            assert chain(_broken)(event) is event
        assert "filter bug" in caplog.text


def test_ignored_telemetry_loggers_matches_the_fleet_tuple():
    assert IGNORED_TELEMETRY_LOGGERS == (
        "opentelemetry.context",
        "opentelemetry.exporter.otlp.proto.grpc.exporter",
        "opentelemetry.sdk.trace.export",
    )


CONNECTION_FAILURE = ["db-infra", "postgres-connection-failure"]


class TestDbInfraFingerprints:
    """One shared-Postgres bounce minted ~15 issues across the fleet; these pin the whole fan-out to one."""

    @pytest.mark.parametrize("signature", DB_CONNECT_SIGNATURES)
    def test_every_connect_signature_pins_one_fingerprint(self, signature):
        event = serialized(("OperationalError", f"psycopg.OperationalError: {signature} (host pgbouncer)"))
        assert fingerprint_db_infra_errors(event)["fingerprint"] == CONNECTION_FAILURE

    def test_the_two_signatures_the_rca_added(self):
        # Seen in gateway-service and absent from every existing copy of the list.
        assert "terminating connection due to administrator command" in DB_CONNECT_SIGNATURES
        assert "canceling statement due to statement timeout" not in DB_CONNECT_SIGNATURES

    def test_read_only_gets_its_own_fingerprint(self):
        event = serialized(("InternalError", "cannot execute UPDATE in a read-only transaction"))
        assert fingerprint_db_infra_errors(event)["fingerprint"] == ["db-infra", "postgres-read-only-transaction"]

    def test_read_only_wins_over_a_connect_match(self):
        """Read-only is the more specific diagnosis; it is checked first so a failover does not read as a bounce."""
        text = "in a read-only transaction; the connection is closed"
        assert fingerprint_db_infra_errors(serialized(("Error", text)))["fingerprint"] == [
            "db-infra",
            "postgres-read-only-transaction",
        ]

    def test_matches_through_the_hint_exception_chain(self):
        exc = OSError("connection failed: connection to server at 10.0.0.5, port 6432 failed")
        assert fingerprint_db_infra_errors({}, {"originalException": exc})["fingerprint"] == CONNECTION_FAILURE

    def test_an_application_error_is_left_alone(self):
        event = serialized(("ValueError", "asset 42 has no serial number"))
        assert "fingerprint" not in fingerprint_db_infra_errors(event)

    def test_an_existing_fingerprint_wins(self):
        event = {"fingerprint": ["mine"], **serialized(("OperationalError", "the connection is closed"))}
        assert fingerprint_db_infra_errors(event)["fingerprint"] == ["mine"]

    def test_a_statement_timeout_is_not_a_connection_failure(self):
        """The judgement call: a timeout is as often one slow query, so it must not land in the db-outage issue."""
        event = serialized(("OperationalError", DB_STATEMENT_TIMEOUT_SIGNATURES[0]))
        assert "fingerprint" not in fingerprint_db_infra_errors(event)

    def test_the_opt_in_timeout_filter_uses_a_separate_fingerprint(self):
        event = serialized(("OperationalError", f"psycopg.errors.QueryCanceled: {DB_STATEMENT_TIMEOUT_SIGNATURES[0]}"))
        assert fingerprint_db_statement_timeouts(event)["fingerprint"] == ["db-infra", "postgres-statement-timeout"]

    def test_the_opt_in_timeout_filter_ignores_everything_else(self):
        assert "fingerprint" not in fingerprint_db_statement_timeouts(serialized(("ValueError", "boom")))

    def test_the_lists_are_immutable_tuples(self):
        # A service importing these must not be able to mutate the fleet-wide list for every other importer.
        for signatures in (DB_CONNECT_SIGNATURES, DB_READONLY_SIGNATURES, DB_STATEMENT_TIMEOUT_SIGNATURES):
            assert isinstance(signatures, tuple)


EXECUTOR_TEARDOWN = ["asgi-shutdown", "current-thread-executor-quit"]
EXECUTOR_MESSAGE = "CurrentThreadExecutor already quit or is broken"


def _executor_exc_info():
    try:
        raise RuntimeError(EXECUTOR_MESSAGE)
    except RuntimeError as exc:
        return (RuntimeError, exc, exc.__traceback__)


class TestAsgiExecutorTeardownFingerprint:
    """asgiref#526 arrives twice per disconnect and fragments per view/logger; both shapes must land in one issue."""

    def test_djangointegration_exception_event(self):
        # DjangoIntegration's capture: exc_info in the hint, the serialized value on the event, a per-view transaction.
        exc_info = _executor_exc_info()
        event = {"transaction": "/api/v1/assets/", **serialized(("RuntimeError", EXECUTOR_MESSAGE))}
        assert fingerprint_asgi_executor_teardown(event, {"exc_info": exc_info})["fingerprint"] == EXECUTOR_TEARDOWN

    def test_uvicorn_logger_message_event(self):
        # uvicorn's `logger.error("Exception in ASGI application", exc_info=...)` through LoggingIntegration.
        exc_info = _executor_exc_info()
        record = logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application\n", (), exc_info
        )
        event = {
            "logger": "uvicorn.error",
            "logentry": {"message": "Exception in ASGI application\n", "params": []},
            **serialized(("RuntimeError", EXECUTOR_MESSAGE)),
        }
        hint = {"exc_info": exc_info, "log_record": record}
        assert fingerprint_asgi_executor_teardown(event, hint)["fingerprint"] == EXECUTOR_TEARDOWN

    def test_matches_on_the_serialized_value_alone(self):
        # A logging-integration event can reach before_send with no exc_info in the hint.
        assert fingerprint_asgi_executor_teardown(serialized(("RuntimeError", EXECUTOR_MESSAGE)))["fingerprint"] == (
            EXECUTOR_TEARDOWN
        )

    def test_both_shapes_share_one_fingerprint_ahead_of_the_template_grouper(self):
        """In chain order the uvicorn log must not fall into the generic uvicorn.error/RuntimeError issue."""
        before_send = chain(drop_cancelled_errors, fingerprint_asgi_executor_teardown, group_log_events_by_template)
        exc_info = _executor_exc_info()
        record = logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application\n", (), exc_info
        )
        django_event = before_send(serialized(("RuntimeError", EXECUTOR_MESSAGE)), {"exc_info": exc_info})
        uvicorn_event = before_send(
            {"logger": "uvicorn.error", **serialized(("RuntimeError", EXECUTOR_MESSAGE))},
            {"exc_info": exc_info, "log_record": record},
        )
        assert django_event["fingerprint"] == uvicorn_event["fingerprint"] == EXECUTOR_TEARDOWN

    def test_is_not_dropped(self):
        # Fingerprinted, never dropped: the same text also covers a genuinely broken executor.
        assert fingerprint_asgi_executor_teardown(serialized(("RuntimeError", EXECUTOR_MESSAGE))) is not None

    def test_an_unrelated_runtime_error_is_left_alone(self):
        event = serialized(("RuntimeError", "dictionary changed size during iteration"))
        assert "fingerprint" not in fingerprint_asgi_executor_teardown(event)

    def test_an_existing_fingerprint_wins(self):
        event = {"fingerprint": ["mine"], **serialized(("RuntimeError", EXECUTOR_MESSAGE))}
        assert fingerprint_asgi_executor_teardown(event)["fingerprint"] == ["mine"]

    def test_signature_matches_asgiref_wording(self):
        assert ASGI_EXECUTOR_TEARDOWN_SIGNATURES == (EXECUTOR_MESSAGE,)
