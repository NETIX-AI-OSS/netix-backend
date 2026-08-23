"""The shared viewset mixins, unit by unit and through routed viewsets on the DRF test client."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from django.core.exceptions import FieldDoesNotExist, FieldError
from django.db.models.signals import post_save
from django.test import override_settings
from envoy_pyauth.utils import EnvoyQueryFilter
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.test import APIClient

from netix_backend.django import views
from netix_backend.django.views import (
    AtomicWriteMixin,
    CreateListModelMixin,
    EnvoyScopedQuerysetMixin,
    IncludeDeletedMixin,
    ObjectOrgOwnership,
    SoftDeleteMixin,
    TenantWriteScopeMixin,
    UpdateListModelMixin,
    include_deleted_schema,
    is_superuser,
    tenant_organization_id,
    write_permissions,
)
from tests import urls as test_urls
from tests.models import GuardedWidget, PlainWidget, RelatedOrgWidget, ScopedWidget

TENANT = 7
OTHER_TENANT = 9


def envoy(organization: Any = TENANT, **extra: Any) -> dict[str, str]:
    """The header the test viewsets turn into ``request.envoy``."""
    payload: dict[str, Any] = {"organization": organization, "permissions": [], **extra}
    return {"HTTP_X_TEST_ENVOY": json.dumps(payload)}


class FakeRequest:
    """Stands in for an authenticated request in the unit-level tests."""

    def __init__(self, envoy_payload: Any = None, method: str = "PATCH", **query: str) -> None:
        self.envoy = envoy_payload
        self.method = method
        self.query_params = query


class FakeView:
    def __init__(self, **attributes: Any) -> None:
        self.__dict__.update(attributes)


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def widget(db: Any) -> ScopedWidget:
    return ScopedWidget.objects.create(label="live", organization_id=TENANT)


def test_accepts_include_shared_detects_the_installed_signature() -> None:
    assert views._accepts_include_shared(EnvoyQueryFilter.get_queryset) is True
    assert views._accepts_include_shared(lambda request, model, session_customer_filter: None) is False
    # An object with no introspectable signature is treated as the older library.
    assert views._accepts_include_shared(object()) is False


@pytest.mark.parametrize(
    ("model", "field_name", "expected"),
    [
        (ScopedWidget, "organization_id", True),
        (ScopedWidget, "label__icontains", True),
        (PlainWidget, "organization_id", False),
        (PlainWidget, "label", True),
        (RelatedOrgWidget, "organization_id", True),
        (RelatedOrgWidget, "missing", False),
        (object(), "organization_id", False),
    ],
)
def test_model_has_field(model: Any, field_name: str, expected: bool) -> None:
    assert views._model_has_field(model, field_name) is expected


def test_serializer_model_unwraps_many_and_falls_back() -> None:
    serializer = test_urls.ScopedWidgetSerializer(many=True)
    assert views._serializer_model(serializer) is ScopedWidget
    assert views._serializer_model(test_urls.ScopedWidgetSerializer) is ScopedWidget
    assert views._serializer_model(FakeView(model=PlainWidget)) is PlainWidget
    assert views._serializer_model(FakeView()) is None


@pytest.mark.parametrize(
    ("request_object", "expected"),
    [
        (FakeRequest({"is_superuser": "TRUE"}), True),
        (FakeRequest({"is_superuser": "no"}), False),
        (FakeRequest({"is_superuser": True}), True),
        (FakeRequest({"organization": 1}), False),
        (FakeRequest(None), False),
        (FakeView(user=FakeView(is_authenticated=True, is_superuser=True)), True),
        (FakeView(), False),
    ],
)
def test_is_superuser(request_object: Any, expected: bool) -> None:
    assert is_superuser(request_object) is expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, None),
        ({"organization": 0}, None),
        ({"organization": "0"}, None),
        ({"organization": "bogus"}, None),
        ({"organization": None}, None),
        ({"organization": TENANT}, TENANT),
        ({"organization": str(TENANT)}, TENANT),
        ({"organization": TENANT, "is_superuser": True}, None),
    ],
)
def test_tenant_organization_id(payload: Any, expected: int | None) -> None:
    assert tenant_organization_id(FakeRequest(payload)) == expected


def test_tenant_organization_id_fails_closed_on_a_malformed_claim() -> None:
    with pytest.raises(PermissionDenied):
        tenant_organization_id(FakeRequest({"organization": "seven"}))


def test_write_permissions_maps_every_write_action() -> None:
    assert write_permissions("widget-edit") == {
        "create": "widget-edit",
        "update": "widget-edit",
        "partial_update": "widget-edit",
        "destroy": "widget-edit",
    }


class ScopeBase:
    def get_queryset(self) -> str:
        return "from-super"


class ScopeHarness(EnvoyScopedQuerysetMixin, ScopeBase):
    def __init__(self, request: Any = None) -> None:
        self.request = request


def test_session_customer_filter_resolves_lazily_from_settings() -> None:
    harness = ScopeHarness()
    assert harness.resolve_session_customer_filter() is True
    with override_settings(SESSION_CUSTOMER_FILTER=False):
        assert harness.resolve_session_customer_filter() is False


def test_session_customer_filter_honours_an_explicit_class_value() -> None:
    class Unscoped(ScopeHarness):
        session_customer_filter = False

    assert Unscoped().resolve_session_customer_filter() is False


def test_optional_organization_field_disables_scoping_for_orgless_models() -> None:
    class Optional(ScopeHarness):
        organization_field_optional = True

    assert Optional().resolve_session_customer_filter(PlainWidget) is False
    assert Optional().resolve_session_customer_filter(ScopedWidget) is True


def test_orgless_model_without_the_opt_in_still_reaches_the_library(db: Any) -> None:
    harness = ScopeHarness(FakeRequest({"organization": TENANT}))
    with pytest.raises(FieldError):
        harness._get_queryset_filter(PlainWidget)


def test_scoping_forwards_include_shared_only_when_the_library_takes_it(
    db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, Any]] = []

    def with_include_shared(
        request: Any,
        model: Any,
        session_customer_filter: bool,
        field_name: str = "",
        delete_filter: bool = True,
        include_shared: bool | None = None,
    ) -> Any:
        seen.append({"include_shared": include_shared})
        return model.objects.all()

    def without_include_shared(
        request: Any, model: Any, session_customer_filter: bool, field_name: str = "", delete_filter: bool = True
    ) -> Any:
        seen.append({"include_shared": "absent"})
        return model.objects.all()

    harness = ScopeHarness(FakeRequest({"organization": TENANT}))
    monkeypatch.setattr(EnvoyQueryFilter, "get_queryset", staticmethod(with_include_shared))
    harness._get_queryset_filter(ScopedWidget, include_shared=True)
    monkeypatch.setattr(EnvoyQueryFilter, "get_queryset", staticmethod(without_include_shared))
    harness._get_queryset_filter(ScopedWidget, include_shared=True)
    assert seen == [{"include_shared": True}, {"include_shared": "absent"}]


def test_scoping_applies_the_additional_filter(db: Any) -> None:
    ScopedWidget.objects.create(label="keep", organization_id=TENANT)
    ScopedWidget.objects.create(label="drop", organization_id=TENANT)
    harness = ScopeHarness(FakeRequest({"organization": TENANT}))
    queryset = harness._get_queryset_filter(ScopedWidget, additional_filter={"label": "keep"})
    assert [row.label for row in queryset] == ["keep"]
    scoped = harness._scope_queryset(ScopedWidget.objects.all(), additional_filter={"label": "drop"})
    assert [row.label for row in scoped] == ["drop"]


def test_get_queryset_defers_to_super_without_a_model_queryset() -> None:
    assert ScopeHarness().get_queryset() == "from-super"


def test_get_queryset_returns_nothing_while_the_schema_is_generated(db: Any) -> None:
    class Declarative(ScopeHarness):
        model_queryset = ScopedWidget.objects.all()

    assert list(Declarative(None).get_queryset()) == []


def test_get_queryset_clones_the_declared_queryset(db: Any, widget: ScopedWidget) -> None:
    class Declarative(ScopeHarness):
        model_queryset = ScopedWidget.objects.all()

    harness = Declarative(FakeRequest({"organization": TENANT}))
    first = harness.get_queryset()
    assert list(first) == [widget]
    assert first is not Declarative.model_queryset


def test_crud_round_trip(client: APIClient, db: Any) -> None:
    created = client.post("/api/widgets/", {"label": "new"}, format="json", **envoy())
    assert created.status_code == 201
    widget_id = created.data["id"]

    listed = client.get("/api/widgets/", **envoy())
    assert [row["id"] for row in listed.data["results"]] == [widget_id]

    fetched = client.get(f"/api/widgets/{widget_id}/", **envoy())
    assert fetched.data["label"] == "new"

    updated = client.put(f"/api/widgets/{widget_id}/", {"label": "changed"}, format="json", **envoy())
    assert updated.data["label"] == "changed"

    patched = client.patch(f"/api/widgets/{widget_id}/", {"label": "patched"}, format="json", **envoy())
    assert patched.data["label"] == "patched"


def test_reads_are_scoped_to_the_calling_organization(client: APIClient, widget: ScopedWidget) -> None:
    ScopedWidget.objects.create(label="theirs", organization_id=OTHER_TENANT)
    listed = client.get("/api/widgets/", **envoy())
    assert [row["label"] for row in listed.data["results"]] == ["live"]


def test_an_unresolved_identity_sees_nothing(client: APIClient, widget: ScopedWidget) -> None:
    assert client.get("/api/widgets/").data["results"] == []


def test_pin_overrides_the_submitted_organization(client: APIClient, db: Any) -> None:
    response = client.post("/api/widgets/", {"label": "x", "organization_id": OTHER_TENANT}, format="json", **envoy())
    assert response.status_code == 201
    assert ScopedWidget.objects.get(pk=response.data["id"]).organization_id == TENANT


def test_scope_off_leaves_the_body_alone(client: APIClient, db: Any) -> None:
    response = client.post("/api/unpinned/", {"label": "x", "organization_id": OTHER_TENANT}, format="json", **envoy())
    assert ScopedWidget.objects.get(pk=response.data["id"]).organization_id == OTHER_TENANT


def test_platform_callers_keep_body_control(client: APIClient, db: Any) -> None:
    response = client.post(
        "/api/widgets/", {"label": "x", "organization_id": OTHER_TENANT}, format="json", **envoy(organization=0)
    )
    assert ScopedWidget.objects.get(pk=response.data["id"]).organization_id == OTHER_TENANT


def test_pin_and_reject_refuses_a_foreign_organization(client: APIClient, db: Any) -> None:
    response = client.post(
        "/api/pin-reject/", {"label": "x", "organization_id": OTHER_TENANT}, format="json", **envoy()
    )
    assert response.status_code == 403
    assert ScopedWidget.objects.count() == 0


def test_pin_and_reject_accepts_the_callers_own_organization(client: APIClient, db: Any) -> None:
    response = client.post("/api/pin-reject/", {"label": "x", "organization_id": TENANT}, format="json", **envoy())
    assert response.status_code == 201


def test_pin_and_reject_guards_updates(client: APIClient, widget: ScopedWidget) -> None:
    response = client.patch(
        f"/api/pin-reject/{widget.pk}/", {"organization_id": OTHER_TENANT}, format="json", **envoy()
    )
    assert response.status_code == 403


def test_pin_and_reject_allows_a_clean_update(client: APIClient, widget: ScopedWidget) -> None:
    response = client.patch(f"/api/pin-reject/{widget.pk}/", {"label": "ok"}, format="json", **envoy())
    assert response.status_code == 200


def test_a_model_without_an_organization_column_is_never_pinned(client: APIClient, db: Any) -> None:
    response = client.post("/api/orgless/", {"label": "x"}, format="json", **envoy())
    assert response.status_code == 201
    assert PlainWidget.objects.count() == 1


def test_exempt_actions_skip_pinning() -> None:
    class Exempt(TenantWriteScopeMixin):
        tenant_write_exempt_actions = frozenset({"create"})

        def __init__(self) -> None:
            self.request = FakeRequest({"organization": TENANT})
            self.action = "create"

    assert Exempt().tenant_organization_id() is None


def test_foreign_instance_is_refused_on_update() -> None:
    class Scope(TenantWriteScopeMixin):
        tenant_write_scope = "pin_and_reject"

    serializer = FakeView(instance=FakeView(organization_id=OTHER_TENANT))
    with pytest.raises(PermissionDenied):
        Scope()._reject_foreign_instance(serializer, TENANT)
    Scope()._reject_foreign_instance(FakeView(instance=FakeView()), TENANT)


def test_foreign_body_rows_are_checked_one_by_one() -> None:
    scope = TenantWriteScopeMixin()
    scope._reject_foreign_body(FakeView(validated_data=[{"organization_id": TENANT}, {}]), TENANT)
    with pytest.raises(PermissionDenied):
        scope._reject_foreign_body(FakeView(validated_data=[{"organization_id": OTHER_TENANT}]), TENANT)


def test_pinning_before_validation_rewrites_the_incoming_body(client: APIClient, db: Any) -> None:
    response = client.post("/api/pin-early/", {"label": "x", "organization_id": OTHER_TENANT}, format="json", **envoy())
    assert response.data["organization_id"] == TENANT


def test_pinning_before_validation_leaves_lists_and_platform_calls_alone(client: APIClient, db: Any) -> None:
    view = test_urls.PinBeforeValidationViewSet()
    view.request = FakeRequest({"organization": TENANT})
    view.action = "create"
    assert view._pinned_write_data([{"label": "x"}]) == [{"label": "x"}]
    view.request = FakeRequest({"organization": 0})
    assert view._pinned_write_data({"label": "x"}) == {"label": "x"}


def test_pinning_before_validation_skips_models_without_the_column() -> None:
    view = test_urls.OrglessViewSet()
    view.tenant_write_pin_before_validation = True
    view.request = FakeRequest({"organization": TENANT})
    view.action = "create"
    assert view._pinned_write_data({"label": "x"}) == {"label": "x"}


def test_destroy_soft_deletes_once(client: APIClient, widget: ScopedWidget) -> None:
    assert client.delete(f"/api/widgets/{widget.pk}/", **envoy()).status_code == 204
    widget.refresh_from_db()
    assert widget.is_deleted is True


def test_repeat_delete_is_a_404_unless_the_view_opts_out(client: APIClient, db: Any) -> None:
    widget = ScopedWidget.objects.create(label="gone", organization_id=TENANT, is_deleted=True)
    assert client.delete(f"/api/keep-deleted/{widget.pk}/", **envoy()).status_code == 204
    assert client.delete(f"/api/repeat-delete/{widget.pk}/", **envoy()).status_code == 404


def test_deleting_a_missing_row_is_a_404(client: APIClient, db: Any) -> None:
    assert client.delete("/api/widgets/4242/", **envoy()).status_code == 404


def test_soft_delete_validation_blocks_the_delete(client: APIClient, db: Any) -> None:
    guarded = GuardedWidget.objects.create(locked=True)
    response = client.delete(f"/api/guarded/{guarded.pk}/", **envoy())
    assert response.status_code == 400
    guarded.refresh_from_db()
    assert guarded.is_deleted is False


def test_soft_delete_falls_back_to_save_when_the_model_has_no_hook() -> None:
    class FakeMeta:
        def get_field(self, name: str) -> Any:
            if name == "updated_on":
                return object()
            raise FieldDoesNotExist(name)

    class TimestampedRow:
        _meta = FakeMeta()

        def __init__(self) -> None:
            self.is_deleted = False
            self.saved: dict[str, Any] = {}

        def save(self, **kwargs: Any) -> None:
            self.saved = kwargs

    class BareRow(TimestampedRow):
        _meta = None

    mixin = SoftDeleteMixin()
    timestamped = TimestampedRow()
    mixin.soft_delete_instance(timestamped)
    assert timestamped.is_deleted is True
    assert timestamped.saved == {"update_fields": ["is_deleted", "updated_on"]}

    bare = BareRow()
    mixin.soft_delete_instance(bare)
    assert bare.saved == {"update_fields": ["is_deleted"]}


def test_soft_delete_validation_is_skipped_when_the_model_has_no_hook() -> None:
    class Validating(SoftDeleteMixin):
        soft_delete_validate = True

    class Row:
        is_deleted = False

        def soft_delete(self) -> None:
            self.is_deleted = True

    row = Row()
    Validating().soft_delete_instance(row)
    assert row.is_deleted is True


@pytest.fixture
def exploding_save() -> Any:
    def receiver(sender: Any, instance: Any, **kwargs: Any) -> None:
        if getattr(instance, "boom", False):
            raise APIException("save exploded")

    post_save.connect(receiver, sender=ScopedWidget)
    yield receiver
    post_save.disconnect(receiver, sender=ScopedWidget)


def test_a_failing_write_rolls_back(client: APIClient, db: Any, exploding_save: Any) -> None:
    response = client.post("/api/widgets/", {"label": "x", "boom": True}, format="json", **envoy())
    assert response.status_code == 500
    assert ScopedWidget.objects.count() == 0


def test_clearing_atomic_actions_keeps_the_partial_write(client: APIClient, db: Any, exploding_save: Any) -> None:
    response = client.post("/api/no-atomic/", {"label": "x", "boom": True}, format="json", **envoy())
    assert response.status_code == 500
    assert ScopedWidget.objects.count() == 1


def test_reads_are_not_wrapped(client: APIClient, widget: ScopedWidget) -> None:
    assert client.get("/api/widgets/", **envoy()).status_code == 200


def test_dispatch_without_an_action_map_wraps_nothing() -> None:
    class Base:
        def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> str:
            return "dispatched"

    class Harness(AtomicWriteMixin, Base):
        def __init__(self) -> None:
            self.post = lambda *args, **kwargs: None

    harness = Harness()
    original = harness.post
    assert harness.dispatch(FakeRequest(method="POST")) == "dispatched"
    assert harness.post is original


def test_object_ownership_modes() -> None:
    permission = ObjectOrgOwnership()
    tenant = FakeRequest({"organization": TENANT, "permissions": []})
    owned = FakeView(organization_id=TENANT)
    foreign = FakeView(organization_id=OTHER_TENANT)
    unowned = FakeView()

    library = FakeView(org_ownership="library", action="partial_update", org_ownership_exempt_actions=())
    assert permission.has_object_permission(tenant, library, owned) is True
    assert permission.has_object_permission(tenant, library, foreign) is False
    assert permission.has_object_permission(tenant, library, unowned) is False

    legacy = FakeView(org_ownership="legacy_open", action="partial_update", org_ownership_exempt_actions=())
    assert permission.has_object_permission(tenant, legacy, unowned) is True
    assert permission.has_object_permission(tenant, legacy, foreign) is False

    off = FakeView(org_ownership="off", action="partial_update")
    assert permission.has_object_permission(tenant, off, foreign) is True

    exempt = FakeView(
        org_ownership="library", action="partial_update", org_ownership_exempt_actions=("partial_update",)
    )
    assert permission.has_object_permission(tenant, exempt, foreign) is True


def test_legacy_ownership_fails_open() -> None:
    obj = FakeView(organization_id=OTHER_TENANT)
    view = FakeView(org_ownership="legacy_open", action="partial_update", org_ownership_exempt_actions=())
    permission = ObjectOrgOwnership()
    assert permission.has_object_permission(FakeRequest(None, method="GET"), view, obj) is True
    assert permission.has_object_permission(FakeRequest(None), view, obj) is True
    assert permission.has_object_permission(FakeRequest({"organization": 0}), view, obj) is True
    assert (
        permission.has_object_permission(FakeRequest({"organization": TENANT, "is_superuser": "true"}), view, obj)
        is True
    )
    assert permission.has_object_permission(FakeRequest({"organization": str(OTHER_TENANT)}), view, obj) is True


def test_library_ownership_blocks_a_foreign_row_through_the_router(client: APIClient, db: Any) -> None:
    foreign = ScopedWidget.objects.create(label="theirs", organization_id=OTHER_TENANT)
    response = client.patch(f"/api/owned-library/{foreign.pk}/", {"label": "mine"}, format="json", **envoy())
    assert response.status_code == 403


def test_legacy_ownership_admits_an_unowned_row_the_library_refuses(client: APIClient, db: Any) -> None:
    plain = PlainWidget.objects.create(label="unowned")
    strict = client.patch(f"/api/owned-plain/{plain.pk}/", {"label": "a"}, format="json", **envoy())
    lenient = client.patch(f"/api/owned-plain-legacy/{plain.pk}/", {"label": "b"}, format="json", **envoy())
    exempt = client.patch(f"/api/owned-exempt/{plain.pk}/", {"label": "c"}, format="json", **envoy())
    assert (strict.status_code, lenient.status_code, exempt.status_code) == (403, 200, 200)


def test_action_permissions_gate_writes(client: APIClient, db: Any) -> None:
    assert client.get("/api/gated/").status_code == 403
    assert client.get("/api/gated/", **envoy()).status_code == 200
    denied = client.post("/api/gated/", {"label": "x"}, format="json", **envoy())
    assert denied.status_code == 403
    allowed = client.post("/api/gated/", {"label": "x"}, format="json", **envoy(permissions=["widget-edit"]))
    assert allowed.status_code == 201


def test_required_permission_resolution_and_authenticate_header() -> None:
    view = test_urls.PermissionViewSet()
    view.action = "create"
    view.request = FakeRequest({}, method="POST")
    assert view.get_required_permission() == "widget-edit"
    # Default None keeps DRF's 403 coercion; setting the knob restores notification-service's real 401s.
    assert view.get_authenticate_header(view.request) is None
    view.authenticate_header = views.WWW_AUTHENTICATE_HEADER
    assert view.get_authenticate_header(view.request) == views.WWW_AUTHENTICATE_HEADER


def test_list_errors_become_validation_errors(client: APIClient, db: Any) -> None:
    response = client.get("/api/list-errors/", **envoy())
    assert response.status_code == 400
    assert "Invalid query" in str(response.data["messages"])


def test_a_predicate_selects_the_mapped_message(client: APIClient, db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_urls.BoomFilterBackend, "error", RuntimeError("integer out of range"))
    response = client.get("/api/list-errors/", **envoy())
    assert response.status_code == 400
    assert "integer out of range" in str(response.data["messages"])


def test_an_unmapped_error_is_re_raised(client: APIClient, db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_urls.BoomFilterBackend, "error", RuntimeError("something else"))
    with pytest.raises(RuntimeError, match="something else"):
        client.get("/api/list-errors/", **envoy())


def test_errors_raised_after_filtering_are_mapped_too(
    client: APIClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(test_urls.BoomFilterBackend, "filter_queryset", lambda self, r, qs, v: qs)

    def exploding_pagination(self: Any, queryset: Any) -> Any:
        raise ValueError("bad offset")

    monkeypatch.setattr(test_urls.ListErrorViewSet, "paginate_queryset", exploding_pagination)
    response = client.get("/api/list-errors/", **envoy())
    assert response.status_code == 400


def test_bulk_create_and_update(client: APIClient, db: Any) -> None:
    created = client.post("/api/bulk/", [{"label": "a"}, {"label": "b"}], format="json", **envoy())
    assert created.status_code == 201
    assert ScopedWidget.objects.count() == 2

    single = client.post("/api/bulk/", {"label": "c"}, format="json", **envoy())
    assert single.status_code == 201

    row = ScopedWidget.objects.get(label="c")
    updated = client.put(f"/api/bulk/{row.pk}/", {"label": "c2"}, format="json", **envoy())
    assert updated.data["label"] == "c2"

    bulk_update = client.put(f"/api/bulk/{row.pk}/", [{"label": "c3"}], format="json", **envoy())
    assert bulk_update.status_code == 200


def test_bulk_update_clears_a_prefetch_cache(client: APIClient, db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    row = ScopedWidget.objects.create(label="cached", organization_id=TENANT)
    row._prefetched_objects_cache = {"stale": []}
    monkeypatch.setattr(test_urls.BulkViewSet, "get_object", lambda self: row)
    response = client.put(f"/api/bulk/{row.pk}/", {"label": "fresh"}, format="json", **envoy())
    assert response.status_code == 200
    assert row._prefetched_objects_cache == {}


def test_bulk_mixins_are_usable_without_the_base() -> None:
    assert issubclass(CreateListModelMixin, object)
    assert issubclass(UpdateListModelMixin, object)


@pytest.fixture
def live_and_dead(db: Any) -> tuple[ScopedWidget, ScopedWidget]:
    dead = ScopedWidget.objects.create(label="same", organization_id=TENANT, is_deleted=True)
    live = ScopedWidget.objects.create(label="same", organization_id=TENANT)
    return live, dead


def test_deleted_rows_stay_hidden_by_default(client: APIClient, live_and_dead: Any) -> None:
    live, _ = live_and_dead
    response = client.get("/api/include-deleted/", **envoy())
    assert [row["id"] for row in response.data["results"]] == [live.pk]


def test_include_deleted_returns_tombstones_live_first(client: APIClient, live_and_dead: Any) -> None:
    live, dead = live_and_dead
    response = client.get("/api/include-deleted/?include_deleted=true", **envoy())
    assert [row["id"] for row in response.data["results"]] == [live.pk, dead.pk]


def test_include_deleted_is_ignored_on_other_actions(client: APIClient, live_and_dead: Any) -> None:
    _, dead = live_and_dead
    assert client.delete(f"/api/include-deleted/{dead.pk}/?include_deleted=true", **envoy()).status_code == 404


def test_include_deleted_falls_back_to_the_declared_queryset(client: APIClient, live_and_dead: Any) -> None:
    live, dead = live_and_dead
    default = client.get("/api/include-deleted-fallback/", **envoy())
    widened = client.get("/api/include-deleted-fallback/?include_deleted=1", **envoy())
    assert [row["id"] for row in default.data["results"]] == [live.pk]
    assert {row["id"] for row in widened.data["results"]} == {live.pk, dead.pk}


def test_include_deleted_fallback_without_envoy_scoping(db: Any) -> None:
    class Bare(IncludeDeletedMixin):
        include_deleted_queryset = ScopedWidget.objects.all()

        def __init__(self) -> None:
            self.action = "list"
            self.request = FakeRequest({}, include_deleted="yes")

    assert list(Bare().get_queryset()) == []


def test_include_deleted_requires_a_request() -> None:
    class Bare(IncludeDeletedMixin):
        def __init__(self) -> None:
            self.action = "list"
            self.request = None

    assert Bare().include_deleted_requested() is False


def test_include_deleted_schema_decorates_the_named_actions() -> None:
    named = include_deleted_schema("list")(type("Named", (test_urls.WidgetViewSet,), {}))
    assert "schema" in named.list.kwargs
    assert "schema" not in getattr(named.partial_update, "kwargs", {})

    default = include_deleted_schema()(type("Default", (test_urls.WidgetViewSet,), {}))
    assert all("schema" in getattr(default, action).kwargs for action in ("list", "retrieve", "update"))


def test_base_viewset_composition_is_stable() -> None:
    assert views.BaseViewSet.pagination_class.__name__ == "BaseLimitOffsetPagination"
    assert "filter_backends" not in views.BaseViewSet.__dict__


ADRF_GUARD = (
    "import os, sys, django; "
    "os.environ['DJANGO_SETTINGS_MODULE'] = 'tests.settings'; "
    "django.setup(); "
    "import netix_backend.django.views; "
    "assert 'adrf' not in sys.modules, sorted(m for m in sys.modules if 'adrf' in m)"
)


def test_views_never_pulls_in_adrf():
    """adrf is an optional extra; the 13 sync-only repos must not acquire it by importing BaseViewSet."""
    subprocess.run([sys.executable, "-c", ADRF_GUARD], check=True, cwd=Path(__file__).resolve().parent.parent)
