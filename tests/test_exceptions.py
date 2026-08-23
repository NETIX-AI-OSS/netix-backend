"""Tests for the shared exception handler, its flatteners and every settings knob."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, OperationalError
from django.http import Http404
from django.test import override_settings
from rest_framework.exceptions import (
    APIException,
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

from netix_backend.django import exceptions
from netix_backend.django.exceptions import (
    DB_UNAVAILABLE_MESSAGE,
    FLATTENERS,
    HTTP404_MESSAGE,
    build_messages,
    custom_exception_handler,
    first_error,
    flatten_first_error,
    flatten_keyed,
    flatten_nested,
    flatten_sequence,
    flatten_template,
    render_messages,
    resolve_flattener,
)

STORMBREAKER_PINS = {
    "NETIX_ERRORS_STRINGIFIED": False,
    "NETIX_ERRORS_HTTP404_AS_LIST": False,
    "NETIX_ERRORS_NON_VALIDATION_AS_LIST": False,
}


class ExcWithMessage(Exception):
    """An exception carrying a `.message` attribute, as several NETIX services raise."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def stub_drf_handler(monkeypatch: pytest.MonkeyPatch, response: Response | None) -> None:
    """Replace DRF's handler so non-API exceptions can be driven through the envelope."""
    monkeypatch.setattr(exceptions, "exception_handler", lambda exc, context: response)


class TestFlattenTemplate:
    def test_bare_string(self) -> None:
        assert flatten_template("boom") == ["boom"]

    def test_message_dict_keeps_only_the_first_entry(self) -> None:
        assert flatten_template({"message": ["one", "two"]}) == ["one"]

    def test_message_dict_with_scalar_value(self) -> None:
        assert flatten_template({"message": "one"}) == ["one"]

    def test_message_dict_with_empty_list_falls_through(self) -> None:
        assert flatten_template({"message": []}) == ["{'message': []}"]

    def test_other_dict_is_stringified_whole(self) -> None:
        assert flatten_template({"other": ["x"]}) == ["{'other': ['x']}"]

    def test_non_dict_non_string(self) -> None:
        assert flatten_template(7) == ["7"]


class TestFlattenKeyed:
    def test_bare_string(self) -> None:
        assert flatten_keyed("boom") == ["boom"]

    def test_message_dict_keeps_every_entry(self) -> None:
        assert flatten_keyed({"message": ["one", "two"]}) == ["one", "two"]

    def test_message_dict_with_scalar_value(self) -> None:
        assert flatten_keyed({"message": 5}) == ["5"]

    def test_field_dict(self) -> None:
        assert flatten_keyed({"name": ["required"], "age": "bad"}) == ["name - ['required']", "age - bad"]

    def test_non_dict_non_string(self) -> None:
        assert flatten_keyed(7) == ["7"]


class TestFirstError:
    def test_sequence_takes_the_first_item(self) -> None:
        assert first_error(["a", "b"]) == "a"

    def test_empty_sequence(self) -> None:
        assert first_error([]) == ""

    def test_nested_dict_flattens_instead_of_repr(self) -> None:
        assert first_error({"child": ["invalid"], "other": "x"}) == "child: invalid, other: x"

    def test_bare_string_is_returned_whole(self) -> None:
        assert first_error("invalid") == "invalid"


class TestFlattenFirstError:
    def test_bare_string(self) -> None:
        assert flatten_first_error("boom") == ["boom"]

    def test_message_dict(self) -> None:
        assert flatten_first_error({"message": ["one", "two"]}) == ["one"]

    def test_field_dict_uses_first_error(self) -> None:
        assert flatten_first_error({"name": [{"inner": ["bad"]}]}) == ["name - inner: bad"]

    def test_non_dict_non_string(self) -> None:
        assert flatten_first_error(7) == ["7"]


class TestFlattenNested:
    def test_bare_string(self) -> None:
        assert flatten_nested("boom") == ["boom"]

    def test_data_service_vector(self) -> None:
        payload = {"field": ["error"], "nested": {"child": ["invalid"]}}
        assert flatten_nested(payload) == ["field: error", "child: invalid"]

    def test_scalar_value(self) -> None:
        assert flatten_nested({"field": "error"}) == ["field: error"]

    def test_non_dict_non_string(self) -> None:
        assert flatten_nested(7) == ["7"]


class TestFlattenSequence:
    def test_bare_string(self) -> None:
        assert flatten_sequence("boom") == ["boom"]

    def test_message_dict(self) -> None:
        assert flatten_sequence({"message": ["one", "two"]}) == ["one"]

    def test_other_dict_is_stringified(self) -> None:
        assert flatten_sequence({"other": 1}) == ["{'other': 1}"]

    def test_recurses_through_sequences(self) -> None:
        assert flatten_sequence([{"message": ["a"]}, ("b",)]) == ["a", "b"]

    def test_non_dict_non_string(self) -> None:
        assert flatten_sequence(7) == ["7"]


class TestResolveFlattener:
    def test_registry_exposes_five_named_flatteners(self) -> None:
        assert sorted(FLATTENERS) == ["first_error", "keyed", "nested", "sequence", "template"]

    def test_default_is_the_template_flattener(self) -> None:
        assert resolve_flattener() is flatten_template

    def test_explicit_name_wins(self) -> None:
        assert resolve_flattener("nested") is flatten_nested

    @override_settings(NETIX_ERRORS_FLATTENER="keyed")
    def test_reads_settings_lazily(self) -> None:
        assert resolve_flattener() is flatten_keyed

    def test_unknown_name_is_a_configuration_error(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="bogus"):
            resolve_flattener("bogus")


class TestBuildMessages:
    def test_validation_error_uses_the_configured_flattener(self) -> None:
        exc = ValidationError({"field": ["error"], "nested": {"child": ["invalid"]}})
        assert build_messages(exc, flattener="nested") == ["field: error", "child: invalid"]

    def test_validation_error_accepts_a_callable(self) -> None:
        assert build_messages(ValidationError("bad"), flattener=flatten_keyed) == ["bad"]

    def test_validation_error_flattens_every_arg(self) -> None:
        assert build_messages(ValidationError("bad")) == ["bad"]

    def test_http404(self) -> None:
        assert build_messages(Http404("No Widget matches the given query.")) == [HTTP404_MESSAGE]

    def test_api_exception_detail(self) -> None:
        assert build_messages(APIException(detail="generic failure")) == ["generic failure"]

    def test_message_attribute_when_detail_is_absent(self) -> None:
        assert build_messages(ExcWithMessage("oops")) == ["oops"]

    def test_falls_back_to_str_of_the_exception(self) -> None:
        assert build_messages(RuntimeError("kaboom")) == ["kaboom"]

    def test_sequence_detail(self) -> None:
        assert build_messages(APIException(detail=["a", "b"])) == ["a", "b"]

    def test_mapping_detail(self) -> None:
        assert build_messages(APIException(detail={"field": "bad"})) == ["field: bad"]


class TestRenderMessages:
    def test_stringified_by_default(self) -> None:
        assert render_messages(["invalid field"]) == "['invalid field']"

    @override_settings(NETIX_ERRORS_STRINGIFIED=False)
    def test_settings_can_ask_for_a_real_array(self) -> None:
        assert render_messages(["invalid field"]) == ["invalid field"]

    def test_explicit_stringified_overrides_settings(self) -> None:
        assert render_messages(["a"], stringified=False) == ["a"]

    def test_as_list_false_collapses_to_the_first_message(self) -> None:
        assert render_messages(["a", "b"], as_list=False) == "a"

    def test_as_list_false_with_no_messages(self) -> None:
        assert render_messages([], as_list=False) == ""


class TestEnvelopeDefaults:
    """The defaults must reproduce today's backend-template wire shape."""

    def test_validation_error_is_a_stringified_list(self) -> None:
        response = custom_exception_handler(ValidationError("invalid field"), {})
        assert response is not None
        assert response.data == {"status_code": 400, "messages": "['invalid field']"}

    def test_http404_is_a_stringified_list(self) -> None:
        response = custom_exception_handler(Http404("nope"), {})
        assert response is not None
        assert response.data == {"status_code": 404, "messages": f"['{HTTP404_MESSAGE}']"}

    def test_non_validation_error_is_a_bare_string(self) -> None:
        # The template family emits str(exc) for 401/403/405/500-class errors, never a stringified list.
        response = custom_exception_handler(NotAuthenticated(), {})
        assert response is not None
        assert response.data == {
            "status_code": 401,
            "messages": "Authentication credentials were not provided.",
        }

    @override_settings(NETIX_ERRORS_NON_VALIDATION_AS_LIST=True)
    def test_non_validation_list_shape_is_opt_in(self) -> None:
        response = custom_exception_handler(NotAuthenticated(), {})
        assert response is not None
        assert response.data["messages"] == "['Authentication credentials were not provided.']"

    def test_unhandled_exception_returns_none(self) -> None:
        assert custom_exception_handler(RuntimeError("boom"), {}) is None

    def test_database_error_is_not_special_cased_by_default(self) -> None:
        assert custom_exception_handler(DatabaseError("connection refused"), {}) is None

    def test_status_code_mirrors_the_drf_response(self) -> None:
        response = custom_exception_handler(PermissionDenied(), {})
        assert response is not None
        assert response.status_code == 403
        assert response.data["status_code"] == 403


class TestStormbreakerUnionPins:
    """The exact seven vectors pinned by stormbreaker_backend/api/tests/test_utils.py."""

    @pytest.fixture(autouse=True)
    def _pins(self) -> Iterator[None]:
        with override_settings(**STORMBREAKER_PINS):
            yield

    def test_validation_error_str(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_drf_handler(monkeypatch, Response({}, status=400))
        handled = custom_exception_handler(ValidationError("bad"), {})
        assert handled is not None
        assert handled.data == {"status_code": 400, "messages": ["bad"]}

    def test_validation_error_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_drf_handler(monkeypatch, Response({}, status=400))
        handled = custom_exception_handler(ValidationError({"message": ["boom"]}), {})
        assert handled is not None
        assert handled.data == {"status_code": 400, "messages": ["boom"]}

    def test_validation_error_other_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_drf_handler(monkeypatch, Response({}, status=400))
        handled = custom_exception_handler(ValidationError({"other": ["x"]}), {})
        assert handled is not None
        assert handled.data == {"status_code": 400, "messages": ["{'other': ['x']}"]}

    def test_non_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_drf_handler(monkeypatch, Response({}, status=500))
        handled = custom_exception_handler(ExcWithMessage("oops"), {})
        assert handled is not None
        assert handled.data == {"status_code": 500, "messages": "oops"}

    def test_not_authenticated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_drf_handler(monkeypatch, Response({}, status=401))
        handled = custom_exception_handler(NotAuthenticated(), {})
        assert handled is not None
        assert handled.data == {
            "status_code": 401,
            "messages": "Authentication credentials were not provided.",
        }

    def test_http404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_drf_handler(monkeypatch, Response({}, status=404))
        handled = custom_exception_handler(Http404("No Tag matches the given query."), {})
        assert handled is not None
        assert handled.data == {"status_code": 404, "messages": HTTP404_MESSAGE}

    @pytest.mark.parametrize(
        ("exc", "status"),
        [
            (NotAuthenticated(), 401),
            (PermissionDenied(), 403),
            (NotFound(), 404),
            (MethodNotAllowed("GET"), 405),
            (Throttled(), 429),
            (Http404("nope"), 404),
        ],
    )
    def test_payload_is_json_renderable(self, monkeypatch: pytest.MonkeyPatch, exc: Exception, status: int) -> None:
        stub_drf_handler(monkeypatch, Response({}, status=status))
        handled = custom_exception_handler(exc, {})
        assert handled is not None
        rendered = JSONRenderer().render(handled.data)
        assert b'"status_code"' in rendered
        assert isinstance(handled.data["messages"], str)


class TestArrayEnvelope:
    """update-service pins messages as a real JSON array."""

    @pytest.fixture(autouse=True)
    def _pins(self) -> Iterator[None]:
        with override_settings(NETIX_ERRORS_STRINGIFIED=False):
            yield

    def test_validation_error_messages_is_a_list_not_string(self) -> None:
        response = custom_exception_handler(ValidationError("invalid field"), {})
        assert response is not None
        assert response.data["messages"] == ["invalid field"]

    def test_multi_field_messages_is_a_list(self) -> None:
        response = custom_exception_handler(ValidationError({"message": ["field required"]}), {})
        assert response is not None
        assert response.data["messages"] == ["field required"]

    def test_exception_without_message_attribute_is_serialisable(self) -> None:
        response = custom_exception_handler(APIException(detail="generic failure"), {})
        assert response is not None
        assert not isinstance(response.data["messages"], Exception)
        json.dumps(response.data)


class TestDatabaseUnavailable:
    """cafm-backend pins a 503 with a BARE STRING message."""

    @pytest.fixture(autouse=True)
    def _pins(self) -> Iterator[None]:
        with override_settings(NETIX_ERRORS_DB_UNAVAILABLE_503=True):
            yield

    def test_database_error_returns_503(self) -> None:
        response = custom_exception_handler(DatabaseError("connection refused"), {})
        assert response is not None
        assert response.status_code == 503
        assert response.data == {"status_code": 503, "messages": DB_UNAVAILABLE_MESSAGE}

    def test_operational_error_returns_503(self) -> None:
        response = custom_exception_handler(OperationalError("server closed the connection"), {})
        assert response is not None
        assert response.status_code == 503
        assert response.data["status_code"] == 503

    def test_other_unhandled_exceptions_still_return_none(self) -> None:
        assert custom_exception_handler(RuntimeError("boom"), {}) is None

    def test_handled_database_error_keeps_the_drf_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub_drf_handler(monkeypatch, Response({}, status=400))
        response = custom_exception_handler(DatabaseError("bad sql"), {})
        assert response is not None
        assert response.status_code == 400


class TestPerRepoFlattenerPins:
    @override_settings(NETIX_ERRORS_FLATTENER="first_error", NETIX_ERRORS_STRINGIFIED=False)
    def test_tag_service_pins(self) -> None:
        response = custom_exception_handler(ValidationError({"name": "invalid"}), {})
        assert response is not None
        assert response.data["messages"] == ["name - invalid"]

    @override_settings(NETIX_ERRORS_FLATTENER="keyed", NETIX_ERRORS_STRINGIFIED=False)
    def test_cafm_pins(self) -> None:
        response = custom_exception_handler(ValidationError({"name": ["invalid"]}), {})
        assert response is not None
        assert response.data["messages"] == ["name - ['invalid']"]

    @override_settings(NETIX_ERRORS_FLATTENER="sequence", NETIX_ERRORS_STRINGIFIED=False)
    def test_static_service_pins(self) -> None:
        response = custom_exception_handler(ValidationError([{"message": ["bad"]}]), {})
        assert response is not None
        assert response.data["messages"] == ["bad"]

    @override_settings(NETIX_ERRORS_FLATTENER="nested", NETIX_ERRORS_STRINGIFIED=False)
    def test_data_service_pins(self) -> None:
        exc = ValidationError({"field": ["error"], "nested": {"child": ["invalid"]}})
        response = custom_exception_handler(exc, {})
        assert response is not None
        assert response.data["messages"] == ["field: error", "child: invalid"]


class TestHttp404AsListKnob:
    @override_settings(NETIX_ERRORS_HTTP404_AS_LIST=False, NETIX_ERRORS_STRINGIFIED=False)
    def test_bare_string(self) -> None:
        response = custom_exception_handler(Http404("nope"), {})
        assert response is not None
        assert response.data["messages"] == HTTP404_MESSAGE

    @override_settings(NETIX_ERRORS_NON_VALIDATION_AS_LIST=False, NETIX_ERRORS_STRINGIFIED=False)
    def test_validation_errors_ignore_the_non_validation_knob(self) -> None:
        response = custom_exception_handler(ValidationError("bad"), {})
        assert response is not None
        assert response.data["messages"] == ["bad"]


def test_handler_delegates_to_drf_first(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, Any]] = []

    def fake(exc: Exception, context: dict[str, Any]) -> Response:
        calls.append((exc, context))
        return Response({}, status=418)

    monkeypatch.setattr(exceptions, "exception_handler", fake)
    exc = ValidationError("x")
    response = custom_exception_handler(exc, {"view": "v"})
    assert response is not None
    assert calls == [(exc, {"view": "v"})]
    assert response.status_code == 418
