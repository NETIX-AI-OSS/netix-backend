"""The on-row change trail: the actor binding, the model mixin, the middleware and the API surface."""

from __future__ import annotations

import contextvars
import datetime
import uuid
from decimal import Decimal
from typing import Any

import pytest
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory

from netix_backend.django.change_history import (
    HISTORY_QUERY_PARAM,
    MAX_REASON_LENGTH,
    REASON_FIELD,
    SOURCE_API,
    SOURCE_MOBILE,
    SOURCE_SYSTEM,
    SOURCE_UPLOAD,
    ChangeActor,
    ChangeHistoryActorMiddleware,
    ChangeHistoryModel,
    actor_context,
    get_actor,
    history_requested,
    json_safe,
    normalize_reason,
    reason_context,
    reason_from_query,
    reset_actor,
    set_actor,
)
from netix_backend.django.change_history_schema import (
    HISTORY_DESCRIPTION,
    HISTORY_PARAMETER,
    REASON_DESCRIPTION,
    REASON_PARAMETER,
    history_parameter,
    reason_parameter,
)
from netix_backend.django.serializers import (
    ChangeHistoryChangeSerializer,
    ChangeHistoryEntrySerializer,
)
from tests.models import (
    CappedHistoryWidget,
    HistoryWidget,
    LooseHistoryWidget,
    UntrackedHistoryWidget,
    Widget,
)
from tests.urls import HistoryWidgetSerializer

HISTORY_URL = "/api/history-widgets/"
PRESENT = 1
ABSENT = 2


@pytest.fixture(autouse=True)
def _unbound_actor():
    """No test may leak an actor into the next one; the contextvar outlives a test function."""
    reset_actor()
    yield
    reset_actor()


def changed(entry: dict[str, Any], field: str) -> dict[str, Any]:
    return next(change for change in entry["changes"] if change["field"] == field)


def fields_of(entry: dict[str, Any]) -> list[str]:
    return [change["field"] for change in entry["changes"]]


class Person:
    """A user-shaped object; the library is duck-typed and never imports django.contrib.auth."""

    def __init__(self, pk: Any = 7, full_name: str = "Rajan Khan", username: str = "rajan"):
        self.pk = pk
        self._full_name = full_name
        self.username = username

    def get_full_name(self) -> str:
        return self._full_name


class TestJsonSafe:
    @pytest.mark.parametrize("value", [None, True, False, 0, 12, 1.5, "text"])
    def test_json_native_values_pass_through_untouched(self, value):
        assert json_safe(value) is value

    def test_temporal_values_become_isoformat_strings(self):
        assert json_safe(datetime.date(2026, 7, 23)) == "2026-07-23"
        assert json_safe(datetime.time(9, 30)) == "09:30:00"
        assert json_safe(datetime.datetime(2026, 7, 23, 9, 30, tzinfo=datetime.UTC)) == "2026-07-23T09:30:00+00:00"

    def test_decimals_become_floats(self):
        assert json_safe(Decimal("12.50")) == 12.5

    def test_sequences_are_coerced_member_by_member(self):
        assert json_safe([Decimal("1"), datetime.date(2026, 1, 1)]) == [1.0, "2026-01-01"]
        assert json_safe((1, 2)) == [1, 2]
        assert json_safe({3}) == [3]

    def test_mappings_keep_string_keys_and_coerced_values(self):
        assert json_safe({1: Decimal("2.5")}) == {"1": 2.5}

    def test_anything_else_falls_back_to_str(self):
        identifier = uuid.uuid4()
        assert json_safe(identifier) == str(identifier)


class TestChangeActor:
    def test_a_user_shaped_object_yields_id_name_and_source(self):
        assert ChangeActor.from_user(Person()) == ChangeActor(user_id=7, name="Rajan Khan", source=SOURCE_API)

    def test_the_source_is_the_caller_s_to_choose(self):
        assert ChangeActor.from_user(Person(), source=SOURCE_MOBILE).source == SOURCE_MOBILE

    def test_a_blank_full_name_falls_back_to_the_username(self):
        assert ChangeActor.from_user(Person(full_name="   ")).name == "rajan"

    def test_a_missing_get_full_name_falls_back_to_the_username(self):
        class Envoyish:
            pk = 3
            username = "svc"

        assert ChangeActor.from_user(Envoyish()) == ChangeActor(user_id=3, name="svc", source=SOURCE_API)

    def test_a_non_integer_pk_is_dropped_rather_than_written_into_the_trail(self):
        # cafm binds from an Envoy payload; an AnonymousUser or a UUID pk must not become a queryable id.
        assert ChangeActor.from_user(Person(pk=None)).user_id is None
        assert ChangeActor.from_user(Person(pk=uuid.uuid4())).user_id is None

    def test_an_object_with_neither_name_nor_username_is_still_an_actor(self):
        assert ChangeActor.from_user(object()) == ChangeActor(user_id=None, name="", source=SOURCE_API)

    def test_the_bare_actor_is_the_anonymous_system_one(self):
        assert ChangeActor() == ChangeActor(user_id=None, name="", source=SOURCE_SYSTEM)

    def test_the_bare_actor_states_no_reason(self):
        assert ChangeActor().reason == ""

    def test_positional_construction_still_means_what_it_did_before_the_reason_field(self):
        # reason is declared last precisely so a consumer pinned to an earlier release keeps working.
        assert ChangeActor(7, "Rajan Khan", SOURCE_API) == ChangeActor(
            user_id=7, name="Rajan Khan", source=SOURCE_API, reason=""
        )

    def test_a_stated_reason_is_normalized_onto_the_actor(self):
        assert ChangeActor.from_user(Person(), reason="  roster swap  ").reason == "roster swap"

    def test_a_user_shaped_object_with_no_stated_reason_states_none(self):
        assert ChangeActor.from_user(Person()).reason == ""


class TestNormalizeReason:
    @pytest.mark.parametrize("raw", [None, "", "   ", 0, False, []])
    def test_anything_falsy_or_blank_collapses_to_the_empty_string(self, raw):
        assert normalize_reason(raw) == ""

    def test_surrounding_whitespace_is_trimmed(self):
        assert normalize_reason("  roster swap  ") == "roster swap"

    def test_a_reason_past_the_cap_is_truncated(self):
        assert normalize_reason("x" * (MAX_REASON_LENGTH + 50)) == "x" * MAX_REASON_LENGTH

    def test_a_non_string_is_coerced_rather_than_refused(self):
        assert normalize_reason(42) == "42"


class TestActorBinding:
    def test_nothing_bound_reads_as_an_anonymous_system_actor(self):
        assert get_actor() == ChangeActor()

    def test_set_then_reset_restores_the_previous_actor(self):
        outer = set_actor(ChangeActor(user_id=1, source=SOURCE_API))
        inner = set_actor(ChangeActor(user_id=2, source=SOURCE_UPLOAD))
        assert get_actor().user_id == 2
        reset_actor(inner)
        assert get_actor().user_id == 1
        reset_actor(outer)
        assert get_actor() == ChangeActor()

    def test_resetting_without_a_token_clears_the_binding(self):
        set_actor(ChangeActor(user_id=5))
        reset_actor()
        assert get_actor().user_id is None

    def test_a_token_minted_in_another_context_clears_instead_of_raising(self):
        # A token cannot cross a context boundary; clearing is the safe floor, never an exception.
        token = contextvars.copy_context().run(set_actor, ChangeActor(user_id=4))
        set_actor(ChangeActor(user_id=6))
        reset_actor(token)
        assert get_actor().user_id is None

    def test_actor_context_binds_a_user_for_the_block(self):
        with actor_context(Person(), source=SOURCE_UPLOAD) as actor:
            assert actor == get_actor()
            assert (actor.user_id, actor.name, actor.source) == (7, "Rajan Khan", SOURCE_UPLOAD)
        assert get_actor() == ChangeActor()

    def test_actor_context_without_a_user_only_stamps_the_source(self):
        with actor_context(source=SOURCE_MOBILE) as actor:
            assert actor == ChangeActor(user_id=None, name="", source=SOURCE_MOBILE)
        assert get_actor() == ChangeActor()

    def test_actor_context_restores_whatever_was_bound_before_it(self):
        set_actor(ChangeActor(user_id=1, source=SOURCE_API))
        with actor_context(Person(), source=SOURCE_UPLOAD):
            pass
        assert get_actor().user_id == 1

    def test_actor_context_carries_a_stated_reason_for_a_user(self):
        with actor_context(Person(), source=SOURCE_UPLOAD, reason="  sheet correction  ") as actor:
            assert actor.reason == "sheet correction"

    def test_actor_context_without_a_user_carries_a_stated_reason_too(self):
        with actor_context(source=SOURCE_MOBILE, reason="  nightly sweep  ") as actor:
            assert actor == ChangeActor(source=SOURCE_MOBILE, reason="nightly sweep")


class TestReasonContext:
    def test_the_reason_is_folded_onto_the_bound_actor_and_then_restored(self):
        # The actor is bound per request; only the reason varies per save.
        set_actor(ChangeActor(user_id=7, name="Rajan Khan", source=SOURCE_API))

        with reason_context("  roster swap  ") as before:
            assert before == ChangeActor(user_id=7, name="Rajan Khan", source=SOURCE_API)
            assert get_actor() == ChangeActor(user_id=7, name="Rajan Khan", source=SOURCE_API, reason="roster swap")

        assert get_actor() == ChangeActor(user_id=7, name="Rajan Khan", source=SOURCE_API)

    def test_with_nobody_bound_the_reason_still_lands_on_the_anonymous_actor(self):
        with reason_context("system sweep"):
            assert get_actor() == ChangeActor(reason="system sweep")

        assert get_actor() == ChangeActor()

    def test_the_previous_reason_is_restored_even_when_the_block_raises(self):
        set_actor(ChangeActor(user_id=7, source=SOURCE_API, reason="original"))

        with pytest.raises(RuntimeError), reason_context("replacement"):
            raise RuntimeError("boom")

        assert get_actor().reason == "original"


class TestReasonFromQuery:
    def test_a_drf_request_states_it_through_query_params(self):
        request = Request(APIRequestFactory().get("/", {REASON_FIELD: "  roster swap  "}))
        assert reason_from_query(request) == "roster swap"

    def test_a_plain_django_request_is_read_through_its_get_dict(self):
        # A DELETE carries no body, so the query string is the only place a reason can arrive.
        assert reason_from_query(RequestFactory().delete(f"/?{REASON_FIELD}=typo")) == "typo"

    def test_an_absent_parameter_is_no_reason(self):
        assert reason_from_query(Request(APIRequestFactory().get("/"))) == ""

    def test_no_request_at_all_is_no_reason(self):
        assert reason_from_query(None) == ""

    def test_an_object_carrying_neither_accessor_is_no_reason(self):
        assert reason_from_query(object()) == ""


class TestHistoryRequested:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
    def test_the_documented_truthy_spellings_opt_in(self, raw):
        request = Request(APIRequestFactory().get("/", {HISTORY_QUERY_PARAM: raw}))
        assert history_requested(request) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "", "off"])
    def test_anything_else_stays_opted_out(self, raw):
        request = Request(APIRequestFactory().get("/", {HISTORY_QUERY_PARAM: raw}))
        assert history_requested(request) is False

    def test_an_absent_parameter_is_opted_out(self):
        assert history_requested(Request(APIRequestFactory().get("/"))) is False

    def test_a_plain_django_request_is_read_through_its_get_dict(self):
        # No DRF Request in sight: the middleware and non-DRF views get the same answer.
        assert history_requested(RequestFactory().get("/", {HISTORY_QUERY_PARAM: "true"})) is True

    def test_no_request_at_all_is_opted_out(self):
        assert history_requested(None) is False

    def test_an_object_carrying_neither_accessor_is_opted_out(self):
        # drf-spectacular introspects serializers with a context that has no request-like object.
        assert history_requested(object()) is False


@pytest.mark.django_db
class TestRecording:
    def test_creation_records_the_starting_values_and_skips_the_empty_ones(self):
        widget = HistoryWidget.objects.create(label="before", status=PRESENT)

        entry = widget.change_history[-1]
        assert (entry["action"], entry["source"], entry["by"], entry["by_name"]) == ("created", SOURCE_SYSTEM, None, "")
        # owner is None so it is skipped; is_deleted is False, which is not one of the empty markers.
        assert sorted(fields_of(entry)) == ["is_deleted", "label", "status"]
        assert changed(entry, "status")["new_display"] == "PRESENT"

    def test_an_edit_records_the_old_and_the_new_value_with_their_labels(self):
        widget = HistoryWidget.objects.create(label="before", status=PRESENT)

        widget.status = ABSENT
        widget.save()

        entry = widget.change_history[-1]
        assert entry["action"] == "updated"
        change = changed(entry, "status")
        assert (change["old"], change["new"]) == (PRESENT, ABSENT)
        assert (change["old_display"], change["new_display"]) == ("PRESENT", "ABSENT")

    def test_a_value_with_no_matching_choice_gets_no_label(self):
        widget = HistoryWidget.objects.create(label="before")

        widget.status = PRESENT
        widget.save()

        change = changed(widget.change_history[-1], "status")
        assert change["new_display"] == "PRESENT"
        assert "old_display" not in change

    def test_a_reload_still_diffs_against_the_stored_row(self):
        widget = HistoryWidget.objects.create(label="before")

        reloaded = HistoryWidget.objects.get(pk=widget.pk)
        reloaded.label = "after"
        reloaded.save()

        assert changed(reloaded.change_history[-1], "label")["old"] == "before"

    def test_refresh_from_db_rebaselines_the_snapshot(self):
        widget = HistoryWidget.objects.create(label="before")
        HistoryWidget.objects.filter(pk=widget.pk).update(label="elsewhere")

        widget.refresh_from_db()
        widget.label = "after"
        widget.save()

        assert changed(widget.change_history[-1], "label")["old"] == "elsewhere"

    def test_a_foreign_key_is_recorded_by_its_id(self):
        first = Widget.objects.create(code="owner-1")
        second = Widget.objects.create(code="owner-2")
        widget = HistoryWidget.objects.create(label="before", owner=first)

        widget.owner = second
        widget.save()

        change = changed(widget.change_history[-1], "owner")
        assert (change["old"], change["new"]) == (first.pk, second.pk)

    def test_untracked_columns_produce_no_entry(self):
        widget = HistoryWidget.objects.create(label="before")

        widget.payload = "bookkeeping"
        widget.save()

        assert len(widget.change_history) == 1

    def test_a_save_that_changes_nothing_produces_no_entry(self):
        widget = HistoryWidget.objects.create(label="before")

        widget.save()

        assert len(widget.change_history) == 1

    def test_a_partial_save_only_records_what_it_persists(self):
        widget = HistoryWidget.objects.create(label="before", status=PRESENT)

        widget.status = ABSENT  # assigned but deliberately not written
        widget.label = "after"
        widget.save(update_fields=["label"])

        assert fields_of(widget.change_history[-1]) == ["label"]
        # Still pending, so the save that does write it is the one that records it.
        widget.save()
        assert changed(widget.change_history[-1], "status")["new"] == ABSENT

    def test_a_partial_save_naming_the_attname_records_the_relation(self):
        owner = Widget.objects.create(code="owner-3")
        widget = HistoryWidget.objects.create(label="before")

        widget.owner = owner
        widget.save(update_fields=["owner_id"])

        assert fields_of(widget.change_history[-1]) == ["owner"]

    def test_a_partial_save_that_touches_nothing_tracked_produces_no_entry(self):
        widget = HistoryWidget.objects.create(label="before")

        widget.label = "after"
        widget.payload = "bookkeeping"
        widget.save(update_fields=["payload"])

        assert len(widget.change_history) == 1
        assert HistoryWidget.objects.get(pk=widget.pk).label == "before"

    def test_a_soft_delete_and_a_restore_are_named_as_such(self):
        widget = HistoryWidget.objects.create(label="before")

        widget.soft_delete()
        assert widget.change_history[-1]["action"] == "deleted"

        widget.is_deleted = False
        widget.save(update_fields=["is_deleted"])
        assert widget.change_history[-1]["action"] == "restored"

    def test_the_trail_is_capped_so_a_row_cannot_grow_without_limit(self):
        widget = CappedHistoryWidget.objects.create(counter=0)

        for counter in range(1, CappedHistoryWidget.history_max_entries + 5):
            widget.counter = counter
            widget.save()

        assert len(widget.change_history) == CappedHistoryWidget.history_max_entries
        # The newest survive; the oldest are the ones dropped.
        newest = CappedHistoryWidget.history_max_entries + 4
        assert changed(widget.change_history[-1], "counter")["new"] == newest

    def test_the_default_cap_is_fifty(self):
        assert ChangeHistoryModel.history_max_entries == 50
        assert HistoryWidget.history_max_entries == 50

    def test_a_bound_actor_is_stamped_onto_the_entry(self):
        widget = HistoryWidget.objects.create(label="before")

        with actor_context(Person(), source=SOURCE_API):
            widget.label = "after"
            widget.save()

        entry = widget.change_history[-1]
        assert (entry["by"], entry["by_name"], entry["source"]) == (7, "Rajan Khan", SOURCE_API)

    def test_an_entry_carries_an_empty_reason_when_nobody_stated_one(self):
        widget = HistoryWidget.objects.create(label="before")

        assert widget.change_history[-1]["reason"] == ""

    def test_a_create_records_the_reason_the_actor_carries(self):
        with actor_context(Person(), source=SOURCE_API, reason="new joiner"):
            widget = HistoryWidget.objects.create(label="before")

        assert widget.change_history[-1]["reason"] == "new joiner"

    def test_an_update_records_the_reason_stated_for_that_one_save(self):
        widget = HistoryWidget.objects.create(label="before")

        with reason_context("roster swap"):
            widget.label = "after"
            widget.save()

        assert [entry["reason"] for entry in widget.change_history] == ["", "roster swap"]

    def test_a_model_without_history_fields_never_records(self):
        widget = UntrackedHistoryWidget.objects.create(label="before")

        widget.label = "after"
        widget.save()

        assert widget.change_history == []

    def test_a_tracked_name_that_is_not_a_column_is_read_off_the_instance(self):
        widget = LooseHistoryWidget.objects.create(label="before")

        created = widget.change_history[-1]
        nickname = changed(created, "nickname")
        assert nickname["new"] == "unset"
        # No field to look choices up on, so neither side gets a display label.
        assert "new_display" not in nickname and "old_display" not in nickname

        widget.nickname = "renamed"
        widget.save()
        assert changed(widget.change_history[-1], "nickname")["new"] == "renamed"

    def test_a_deferred_field_is_left_out_of_the_baseline(self):
        widget = HistoryWidget.objects.create(label="before", status=PRESENT)

        deferred = HistoryWidget.objects.defer("label").get(pk=widget.pk)

        snapshot = deferred._change_history_snapshot
        assert snapshot is not None
        assert "label" not in snapshot
        assert snapshot["status"] == PRESENT

    def test_an_in_memory_instance_saved_as_an_update_has_nothing_to_diff(self):
        stored = HistoryWidget.objects.create(label="before")

        # What a hand-built instance looks like: a pk, no load, therefore no snapshot.
        ghost = HistoryWidget(pk=stored.pk, label="after", created_on=stored.created_on)
        ghost._state.adding = False
        ghost.save()

        assert ghost.change_history == []


@pytest.mark.django_db
class TestMiddleware:
    def test_the_request_starts_with_no_actor_and_the_outer_one_is_restored(self):
        seen: dict[str, ChangeActor] = {}
        set_actor(ChangeActor(user_id=9, source=SOURCE_UPLOAD))

        def view(request):
            seen["actor"] = get_actor()
            return HttpResponse("ok")

        response = ChangeHistoryActorMiddleware(view)(RequestFactory().get("/"))

        assert response.status_code == 200
        assert seen["actor"] == ChangeActor()
        assert get_actor().user_id == 9

    def test_the_actor_is_unbound_even_when_the_view_raises(self):
        def view(request):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            ChangeHistoryActorMiddleware(view)(RequestFactory().get("/"))

        assert get_actor() == ChangeActor()

    def test_a_subclass_binds_whatever_the_request_already_carries(self):
        class EnvoyActorMiddleware(ChangeHistoryActorMiddleware):
            def resolve_actor(self, request):
                return ChangeActor(user_id=int(request.headers["x-user"]), source=SOURCE_API)

        seen: dict[str, ChangeActor] = {}

        def view(request):
            seen["actor"] = get_actor()
            return HttpResponse("ok")

        EnvoyActorMiddleware(view)(RequestFactory().get("/", headers={"x-user": "42"}))

        assert seen["actor"] == ChangeActor(user_id=42, name="", source=SOURCE_API)
        assert get_actor() == ChangeActor()

    async def test_an_async_stack_gets_the_same_bracketing(self):
        seen: dict[str, ChangeActor] = {}
        set_actor(ChangeActor(user_id=9, source=SOURCE_UPLOAD))

        async def view(request):
            seen["actor"] = get_actor()
            return HttpResponse("ok")

        middleware = ChangeHistoryActorMiddleware(view)
        # What convert_exception_to_response reads to decide it may await the middleware.
        assert middleware.async_mode is True
        response = await middleware(RequestFactory().get("/"))

        assert response.status_code == 200
        assert seen["actor"] == ChangeActor()
        assert get_actor().user_id == 9

    async def test_an_async_subclass_binds_too(self):
        class EnvoyActorMiddleware(ChangeHistoryActorMiddleware):
            def resolve_actor(self, request):
                return ChangeActor(user_id=42, source=SOURCE_API)

        seen: dict[str, ChangeActor] = {}

        async def view(request):
            seen["actor"] = get_actor()
            return HttpResponse("ok")

        await EnvoyActorMiddleware(view)(RequestFactory().get("/"))

        assert seen["actor"].user_id == 42
        assert get_actor() == ChangeActor()


@pytest.mark.django_db
class TestSerializerMixin:
    def test_the_field_is_dropped_unless_the_caller_asked_for_it(self):
        widget = HistoryWidget.objects.create(label="before")

        request = Request(APIRequestFactory().get("/"))
        assert "change_history" not in HistoryWidgetSerializer(widget, context={"request": request}).data

    def test_the_field_is_carried_when_the_caller_asked_for_it(self):
        widget = HistoryWidget.objects.create(label="before")

        request = Request(APIRequestFactory().get("/", {HISTORY_QUERY_PARAM: "true"}))
        data = HistoryWidgetSerializer(widget, context={"request": request}).data
        assert data["change_history"][0]["action"] == "created"

    def test_schema_generation_keeps_the_field_but_a_render_still_drops_it(self):
        widget = HistoryWidget.objects.create(label="before")

        class FakeView:
            swagger_fake_view = True

        serializer = HistoryWidgetSerializer(widget, context={"view": FakeView(), "request": None})
        # Declared for the contract...
        assert "change_history" in serializer.fields
        # ...but never rendered for a caller that did not opt in.
        assert "change_history" not in serializer.data

    def test_the_entry_serializer_describes_the_stored_shape(self):
        widget = HistoryWidget.objects.create(label="before", status=PRESENT)

        data = ChangeHistoryEntrySerializer(widget.change_history, many=True).data

        assert set(data[0]) == {"at", "action", "source", "by", "by_name", "reason", "changes"}
        assert data[0]["source"] == SOURCE_SYSTEM
        assert set(ChangeHistoryChangeSerializer().fields) == {"field", "old", "new", "old_display", "new_display"}

    def test_an_entry_recorded_before_the_field_existed_reads_back_as_null(self):
        legacy = {
            "at": "2026-01-01T00:00:00+00:00",
            "action": "updated",
            "source": SOURCE_API,
            "by": 7,
            "by_name": "Rajan Khan",
            "changes": [],
        }

        assert ChangeHistoryEntrySerializer([legacy], many=True).data[0]["reason"] is None

    def test_a_stated_reason_lands_on_the_entry_the_write_produces(self):
        serializer = HistoryWidgetSerializer(data={"label": "before", "change_reason": "  roster swap  "})
        assert serializer.is_valid(), serializer.errors
        # Never in validated_data: ModelSerializer would hand it to HistoryWidget(**validated_data).
        assert REASON_FIELD not in serializer.validated_data

        widget = serializer.save()

        assert widget.change_history[-1]["reason"] == "roster swap"
        # Write-only, so it is never echoed back.
        assert REASON_FIELD not in serializer.data

    def test_a_write_stating_no_reason_saves_exactly_as_it_did_before(self):
        serializer = HistoryWidgetSerializer(data={"label": "before"})
        assert serializer.is_valid(), serializer.errors

        widget = serializer.save()

        assert widget.change_history[-1]["reason"] == ""

    def test_a_blank_reason_is_accepted_and_recorded_as_none_stated(self):
        serializer = HistoryWidgetSerializer(data={"label": "before", "change_reason": ""})
        assert serializer.is_valid(), serializer.errors

        assert serializer.save().change_history[-1]["reason"] == ""

    def test_a_reason_past_the_cap_is_refused_rather_than_silently_truncated(self):
        serializer = HistoryWidgetSerializer(data={"label": "before", "change_reason": "x" * (MAX_REASON_LENGTH + 1)})

        assert serializer.is_valid() is False
        assert REASON_FIELD in serializer.errors


@pytest.mark.django_db
class TestThroughTheApi:
    def test_the_trail_is_absent_unless_it_is_asked_for(self):
        widget = HistoryWidget.objects.create(label="before")

        payload = APIClient().get(f"{HISTORY_URL}{widget.pk}/").json()

        assert "change_history" not in payload

    def test_the_trail_is_returned_when_requested(self):
        widget = HistoryWidget.objects.create(label="before")

        payload = APIClient().get(f"{HISTORY_URL}{widget.pk}/?{HISTORY_QUERY_PARAM}=true").json()

        assert [entry["action"] for entry in payload["change_history"]] == ["created"]

    def test_list_responses_honour_the_flag_too(self):
        HistoryWidget.objects.create(label="before")

        plain = APIClient().get(HISTORY_URL).json()
        opted_in = APIClient().get(f"{HISTORY_URL}?{HISTORY_QUERY_PARAM}=1").json()

        assert "change_history" not in plain[0]
        assert "change_history" in opted_in[0]

    def test_an_api_edit_is_recorded_and_a_client_cannot_forge_the_trail(self):
        widget = HistoryWidget.objects.create(label="before")

        response = APIClient().patch(
            f"{HISTORY_URL}{widget.pk}/?{HISTORY_QUERY_PARAM}=true",
            {"label": "after", "change_history": [{"action": "forged"}]},
            format="json",
        )

        assert response.status_code == 200
        assert [entry["action"] for entry in response.json()["change_history"]] == ["created", "updated"]

    def test_an_api_edit_records_the_reason_the_caller_stated(self):
        widget = HistoryWidget.objects.create(label="before")

        response = APIClient().patch(
            f"{HISTORY_URL}{widget.pk}/?{HISTORY_QUERY_PARAM}=true",
            {"label": "after", "change_reason": "roster swap"},
            format="json",
        )

        payload = response.json()
        assert [entry["reason"] for entry in payload["change_history"]] == ["", "roster swap"]
        assert "change_reason" not in payload


class TestSchemaParameter:
    def test_the_ready_made_parameter_advertises_the_query_flag(self):
        assert HISTORY_PARAMETER.name == HISTORY_QUERY_PARAM
        assert HISTORY_PARAMETER.location == "query"
        assert HISTORY_PARAMETER.required is False
        assert HISTORY_PARAMETER.description == HISTORY_DESCRIPTION

    def test_an_adopter_can_supply_its_own_prose_and_parameter_name(self):
        parameter = history_parameter(description="Include the audit trail.", name="with_history")

        assert (parameter.name, parameter.description) == ("with_history", "Include the audit trail.")


class TestReasonParameter:
    def test_the_ready_made_parameter_advertises_the_body_less_reason(self):
        assert REASON_PARAMETER.name == REASON_FIELD
        assert REASON_PARAMETER.location == "query"
        assert REASON_PARAMETER.required is False
        assert REASON_PARAMETER.description == REASON_DESCRIPTION

    def test_an_adopter_can_supply_its_own_prose_and_parameter_name(self):
        parameter = reason_parameter(description="Why this is going away.", name="why")

        assert (parameter.name, parameter.description) == ("why", "Why this is going away.")
