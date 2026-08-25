"""The superuser cross-org mixin: the union of the four repos' suites, as stubs and through the router."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from netix_backend.django.org_scope import (
    FORBIDDEN_ORGANIZATION,
    INVALID_ORGANIZATION,
    ORGANIZATION_QUERY_PARAM,
    SuperuserOrgScopeMixin,
)
from tests.models import PlainWidget, ScopedWidget
from tests.urls import ScopedWidgetSerializer

TENANT = 7
TARGET = 11

SUPERUSER_ENVOY = {"organization": 0, "is_superuser": "true"}
TENANT_ENVOY = {"organization": TENANT}


def envoy_header(payload: dict[str, Any]) -> dict[str, str]:
    """The header the test viewsets turn into ``request.envoy``."""
    return {"HTTP_X_TEST_ENVOY": json.dumps({"permissions": [], **payload})}


class FakeRequest:
    def __init__(self, envoy_payload: Any = None, **query: str) -> None:
        self.envoy = envoy_payload
        self.query_params = query


class SuperStub:
    """Stands in for the rest of the MRO, so every fallthrough branch is observable by identity."""

    def get_queryset(self) -> Any:
        return "SUPER_QUERYSET"

    def perform_create(self, serializer: Any) -> Any:
        return "SUPER_CREATE"

    def perform_update(self, serializer: Any) -> Any:
        return "SUPER_UPDATE"

    def _get_queryset_filter(self, model: Any, **kwargs: Any) -> Any:
        return ("SCOPED", model, kwargs)


class StubView(SuperuserOrgScopeMixin, SuperStub):
    def __init__(self, request: Any = None, **attributes: Any) -> None:
        self.request = request
        self.__dict__.update(attributes)


def superuser_view(**attributes: Any) -> StubView:
    return StubView(FakeRequest(SUPERUSER_ENVOY, organization=str(TARGET)), **attributes)


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def cross_org_rows(db: Any) -> dict[str, ScopedWidget]:
    """One live row per organization plus a tombstone in the target, so exclusion and scoping both show."""
    return {
        "mine": ScopedWidget.objects.create(label="mine", organization_id=TENANT),
        "theirs": ScopedWidget.objects.create(label="theirs", organization_id=TARGET),
        "theirs_deleted": ScopedWidget.objects.create(label="gone", organization_id=TARGET, is_deleted=True),
    }


def test_no_parameter_means_normal_scoping() -> None:
    assert StubView(FakeRequest(TENANT_ENVOY)).get_queryset() == "SUPER_QUERYSET"


def test_a_request_less_view_never_raises() -> None:
    # drf-spectacular introspects the view with no request bound; data-service and viz crash here today.
    view = StubView()
    assert view.superuser_target_organization_id() is None
    assert view.get_queryset() == "SUPER_QUERYSET"


def test_a_non_integer_is_a_400_keyed_by_the_parameter_name() -> None:
    view = StubView(FakeRequest(SUPERUSER_ENVOY, organization="abc"))
    with pytest.raises(ValidationError) as error:
        view.superuser_target_organization_id()
    assert error.value.detail == {ORGANIZATION_QUERY_PARAM: INVALID_ORGANIZATION}


def test_a_blank_parameter_is_a_400_by_default() -> None:
    with pytest.raises(ValidationError):
        StubView(FakeRequest(SUPERUSER_ENVOY, organization="")).superuser_target_organization_id()


def test_a_blank_parameter_can_be_read_as_absent() -> None:
    view = StubView(FakeRequest(SUPERUSER_ENVOY, organization=""), superuser_org_blank_is_absent=True)
    assert view.superuser_target_organization_id() is None


def test_a_tenant_is_refused_for_another_organization() -> None:
    view = StubView(FakeRequest(TENANT_ENVOY, organization=str(TARGET)))
    with pytest.raises(PermissionDenied) as error:
        view.superuser_target_organization_id()
    assert str(error.value.detail) == FORBIDDEN_ORGANIZATION


def test_a_tenant_is_refused_even_for_its_own_organization() -> None:
    # All four repos reject any non-superuser that sends the parameter at all; three descriptions say otherwise.
    with pytest.raises(PermissionDenied):
        StubView(FakeRequest(TENANT_ENVOY, organization=str(TENANT))).superuser_target_organization_id()


def test_garbage_from_a_tenant_is_a_400_not_a_403() -> None:
    # Parse before the gate, so the caller learns the parameter is malformed rather than that they are not trusted.
    with pytest.raises(ValidationError):
        StubView(FakeRequest(TENANT_ENVOY, organization="abc")).superuser_target_organization_id()


def test_a_direct_django_superuser_is_accepted_too() -> None:
    request = FakeRequest(None, organization=str(TARGET))
    request.user = MagicMock(is_authenticated=True, is_superuser=True)
    assert StubView(request).superuser_target_organization_id() == TARGET


def test_both_messages_are_overridable() -> None:
    view = superuser_view(
        superuser_org_invalid_message="A valid integer is required.",
        superuser_org_forbidden_message="Nope.",
    )
    view.request = FakeRequest(TENANT_ENVOY, organization="abc")
    with pytest.raises(ValidationError) as invalid:
        view.superuser_target_organization_id()
    assert invalid.value.detail == {ORGANIZATION_QUERY_PARAM: "A valid integer is required."}
    view.request = FakeRequest(TENANT_ENVOY, organization=str(TARGET))
    with pytest.raises(PermissionDenied) as forbidden:
        view.superuser_target_organization_id()
    assert str(forbidden.value.detail) == "Nope."


def test_the_query_parameter_name_is_overridable() -> None:
    view = StubView(FakeRequest(SUPERUSER_ENVOY, org=str(TARGET)), superuser_org_query_param="org")
    assert view.superuser_target_organization_id() == TARGET


def test_the_cross_org_read_goes_through_the_default_manager() -> None:
    model = MagicMock()
    queryset = superuser_view(superuser_org_scope_model=model).get_queryset()
    model._default_manager.filter.assert_called_once_with(organization_id=TARGET, is_deleted=False)
    model._default_manager.filter.return_value.order_by.assert_called_once_with("id")
    assert queryset is model._default_manager.filter.return_value.order_by.return_value


def test_the_field_ordering_and_extra_filter_are_overridable() -> None:
    model = MagicMock()
    superuser_view(
        superuser_org_scope_model=model,
        superuser_org_scope_field="asset__organization_id",
        superuser_org_ordering=("level_no", "id"),
        superuser_org_extra_filter={},
    ).get_queryset()
    model._default_manager.filter.assert_called_once_with(asset__organization_id=TARGET)
    model._default_manager.filter.return_value.order_by.assert_called_once_with("level_no", "id")


def test_the_manager_seam_is_overridable() -> None:
    model = MagicMock()

    class ObjectsView(StubView):
        def superuser_org_manager(self, model: Any) -> Any:
            return model.objects

    ObjectsView(FakeRequest(SUPERUSER_ENVOY, organization=str(TARGET)), superuser_org_scope_model=model).get_queryset()
    model.objects.filter.assert_called_once_with(organization_id=TARGET, is_deleted=False)


def test_an_explicit_model_wins_over_the_declared_one() -> None:
    explicit, declared = MagicMock(), MagicMock()
    superuser_view(superuser_org_scope_model=declared).superuser_org_queryset(explicit, organization_id=TARGET)
    explicit._default_manager.filter.assert_called_once()
    declared._default_manager.filter.assert_not_called()


def test_the_model_falls_back_to_the_scoped_queryset() -> None:
    view = superuser_view(model_queryset=ScopedWidget.objects.all())
    assert view.superuser_org_model() is ScopedWidget


def test_the_model_falls_back_to_a_plain_queryset() -> None:
    assert superuser_view(queryset=PlainWidget.objects.all()).superuser_org_model() is PlainWidget


def test_the_model_falls_back_to_the_serializer() -> None:
    assert superuser_view(serializer_class=ScopedWidgetSerializer).superuser_org_model() is ScopedWidget


def test_an_unresolvable_model_raises_a_runtime_error() -> None:
    with pytest.raises(RuntimeError, match="superuser_org_scope_model"):
        superuser_view().get_queryset()


def test_the_helper_surface_scopes_when_untargeted() -> None:
    view = StubView(FakeRequest(TENANT_ENVOY))
    assert view.get_org_scoped_queryset(ScopedWidget, field_name="organization_id") == (
        "SCOPED",
        ScopedWidget,
        {"field_name": "organization_id"},
    )


def test_the_helper_surface_resolves_the_model_when_untargeted() -> None:
    view = StubView(FakeRequest(TENANT_ENVOY), model_queryset=ScopedWidget.objects.all())
    assert view.get_org_scoped_queryset() == ("SCOPED", ScopedWidget, {})


def test_the_helper_surface_crosses_organizations_when_targeted() -> None:
    model = MagicMock()
    superuser_view().get_org_scoped_queryset(model)
    model._default_manager.filter.assert_called_once_with(organization_id=TARGET, is_deleted=False)


def test_the_writes_return_the_super_result_untargeted() -> None:
    # DRF ignores the return value, but asset-service's stub tests assert it, so the mixin keeps returning it.
    view = StubView(FakeRequest(TENANT_ENVOY))
    assert view.perform_create(MagicMock()) == "SUPER_CREATE"
    assert view.perform_update(MagicMock()) == "SUPER_UPDATE"


def test_the_writes_pin_the_target_organization() -> None:
    view = superuser_view()
    serializer = MagicMock()
    assert view.perform_create(serializer) is serializer.save.return_value
    assert view.perform_update(serializer) is serializer.save.return_value
    assert serializer.save.call_args_list == [
        (((), {"organization_id": TARGET})),
        (((), {"organization_id": TARGET})),
    ]


def test_a_superuser_reads_another_organizations_live_rows(client: APIClient, cross_org_rows: Any) -> None:
    response = client.get(f"/api/org-scope/?organization={TARGET}", **envoy_header(SUPERUSER_ENVOY))
    assert response.status_code == 200
    assert [row["id"] for row in response.data["results"]] == [cross_org_rows["theirs"].pk]


def test_a_superuser_without_the_parameter_keeps_its_own_scope(client: APIClient, cross_org_rows: Any) -> None:
    response = client.get("/api/org-scope/", **envoy_header({"organization": TENANT, "is_superuser": "true"}))
    assert [row["id"] for row in response.data["results"]] == [cross_org_rows["mine"].pk]


def test_a_tenant_is_refused_through_the_router(client: APIClient, cross_org_rows: Any) -> None:
    response = client.get(f"/api/org-scope/?organization={TARGET}", **envoy_header(TENANT_ENVOY))
    assert response.status_code == 403


def test_a_non_integer_is_a_400_through_the_router(client: APIClient, cross_org_rows: Any) -> None:
    response = client.get("/api/org-scope/?organization=abc", **envoy_header(SUPERUSER_ENVOY))
    assert response.status_code == 400


def test_a_blank_parameter_through_the_router(client: APIClient, cross_org_rows: Any) -> None:
    strict = client.get("/api/org-scope/?organization=", **envoy_header(SUPERUSER_ENVOY))
    lenient = client.get("/api/org-scope-blank/?organization=", **envoy_header(TENANT_ENVOY))
    assert strict.status_code == 400
    assert lenient.status_code == 200
    assert [row["id"] for row in lenient.data["results"]] == [cross_org_rows["mine"].pk]


def test_a_superuser_creates_into_the_target_organization(client: APIClient, db: Any) -> None:
    response = client.post(
        f"/api/org-scope/?organization={TARGET}",
        {"label": "new", "organization_id": TENANT},
        format="json",
        **envoy_header(SUPERUSER_ENVOY),
    )
    assert response.status_code == 201
    assert ScopedWidget.objects.get(pk=response.data["id"]).organization_id == TARGET


def test_a_superuser_updates_into_the_target_organization(client: APIClient, cross_org_rows: Any) -> None:
    theirs = cross_org_rows["theirs"]
    response = client.patch(
        f"/api/org-scope/{theirs.pk}/?organization={TARGET}",
        {"label": "edited", "organization_id": TENANT},
        format="json",
        **envoy_header(SUPERUSER_ENVOY),
    )
    assert response.status_code == 200
    theirs.refresh_from_db()
    assert (theirs.label, theirs.organization_id) == ("edited", TARGET)


def test_the_helper_surface_through_the_router(client: APIClient, cross_org_rows: Any) -> None:
    crossed = client.get(f"/api/org-scope-helper/?organization={TARGET}", **envoy_header(SUPERUSER_ENVOY))
    scoped = client.get("/api/org-scope-helper/", **envoy_header(TENANT_ENVOY))
    assert [row["id"] for row in crossed.data["results"]] == [cross_org_rows["theirs"].pk]
    assert [row["id"] for row in scoped.data["results"]] == [cross_org_rows["mine"].pk]


def test_the_mixin_advertises_nothing_by_itself() -> None:
    # The invariant the four repos depend on: advertising is the adopter's, in three incompatible mechanisms.
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator(urlconf="tests.urls").get_schema(request=None, public=True)
    parameters = schema["paths"]["/api/org-scope/"]["get"].get("parameters", [])
    assert ORGANIZATION_QUERY_PARAM not in [parameter["name"] for parameter in parameters]
