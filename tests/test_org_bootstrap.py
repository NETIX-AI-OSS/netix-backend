"""The internal org-bootstrap endpoint factory: driver binding, the caller's own CloneError, and the 401/403 guard."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from rest_framework.test import APIRequestFactory

from netix_backend.django.org_bootstrap import (
    CloneError,
    OrgBootstrapBaseView,
    OrgBootstrapSerializer,
    OrgTeardownSerializer,
    build_org_bootstrap_view,
)

PLATFORM = {"organization": 0, "is_superuser": True, "is_platform_internal": True, "permissions": []}
TENANT = {"organization": 7, "is_superuser": False, "permissions": ["anything"]}


class ServiceCloneError(RuntimeError):
    """A service's own CloneError; six repos define one and the factory has to catch that type."""


class Driver:
    """Records the calls the view makes, and raises on the organization the test asks it to refuse."""

    def __init__(self, refuse: int | None = None, error: type[Exception] = ServiceCloneError) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.refuse = refuse
        self.error = error

    def clone(self, target_org: int, source_org: int = 0, run_id: str | None = None) -> dict[str, int]:
        self.calls.append(("clone", target_org, source_org, run_id))
        if target_org == self.refuse:
            raise self.error("an inverted-ownership edge blocks the clone")
        return {"widgets": 2}

    def teardown(self, target_org: int) -> dict[str, int]:
        self.calls.append(("teardown", target_org))
        if target_org == self.refuse:
            raise self.error("rows outside the catalog still reference it")
        return {"widgets": 2}


def call(view_class: type[OrgBootstrapBaseView], method: str, body: dict[str, Any], envoy: Any = PLATFORM) -> Any:
    factory = APIRequestFactory()
    request = getattr(factory, method)("/api/internal/org-bootstrap/", body, format="json")
    request.envoy = envoy
    return view_class.as_view()(request)


@pytest.fixture
def driver() -> Driver:
    return Driver(refuse=409)


@pytest.fixture
def view(driver: Driver) -> type[OrgBootstrapBaseView]:
    return build_org_bootstrap_view(clone=driver.clone, teardown=driver.teardown, error=ServiceCloneError)


class TestFactory:
    def test_names_the_generated_class(self, driver: Driver) -> None:
        built = build_org_bootstrap_view(clone=driver.clone, teardown=driver.teardown, name="AssetOrgBootstrapView")
        assert built.__name__ == "AssetOrgBootstrapView"
        assert issubclass(built, OrgBootstrapBaseView)

    def test_the_default_name_matches_the_six_repo_copies(self, view: type[OrgBootstrapBaseView]) -> None:
        assert view.__name__ == "OrgBootstrapView"

    def test_the_drivers_are_static_not_bound_methods(self, view: type[OrgBootstrapBaseView], driver: Driver) -> None:
        # A plain function attribute would arrive with the view as its target_org.
        assert view().clone_primitives == driver.clone
        assert view().teardown_primitives == driver.teardown

    def test_the_generated_class_carries_no_docstring(self, view: type[OrgBootstrapBaseView]) -> None:
        assert view.__dict__.get("__doc__") is None

    def test_drf_defaults_stay_disabled(self, view: type[OrgBootstrapBaseView]) -> None:
        # The guard is the decorator; DRF's own defaults would 403 before it ran.
        assert view.permission_classes == ()
        assert view.authentication_classes == ()
        assert view.schema is None


class TestPost:
    def test_clones_and_reports_the_stats(self, view: type[OrgBootstrapBaseView], driver: Driver) -> None:
        response = call(view, "post", {"organization_id": 7, "source_organization_id": 0, "run_id": "r1"})
        assert response.status_code == 200
        assert response.data == {"organization_id": 7, "stats": {"widgets": 2}}
        assert driver.calls == [("clone", 7, 0, "r1")]

    def test_a_blank_run_id_reaches_the_driver_as_none(self, view: type[OrgBootstrapBaseView], driver: Driver) -> None:
        call(view, "post", {"organization_id": 7})
        assert driver.calls == [("clone", 7, 0, None)]

    def test_organization_zero_is_rejected_by_the_serializer(self, view: type[OrgBootstrapBaseView]) -> None:
        assert call(view, "post", {"organization_id": 0}).status_code == 400

    def test_the_services_own_clone_error_becomes_a_409(self, view: type[OrgBootstrapBaseView]) -> None:
        response = call(view, "post", {"organization_id": 409})
        assert response.status_code == 409
        assert response.data == {"detail": "an inverted-ownership edge blocks the clone"}

    def test_the_library_clone_error_is_the_default(self, driver: Driver) -> None:
        driver.error = CloneError
        built = build_org_bootstrap_view(clone=driver.clone, teardown=driver.teardown)
        assert call(built, "post", {"organization_id": 409}).status_code == 409


class TestDelete:
    def test_tears_down_and_reports_the_stats(self, view: type[OrgBootstrapBaseView], driver: Driver) -> None:
        response = call(view, "delete", {"organization_id": 7})
        assert response.status_code == 200
        assert response.data == {"organization_id": 7, "stats": {"widgets": 2}}
        assert driver.calls == [("teardown", 7)]

    def test_the_run_id_field_stays_on_the_wire_but_never_reaches_the_driver(
        self, view: type[OrgBootstrapBaseView], driver: Driver
    ) -> None:
        assert call(view, "delete", {"organization_id": 7, "run_id": "r1"}).status_code == 200
        assert driver.calls == [("teardown", 7)]

    def test_a_refused_teardown_is_a_409(self, view: type[OrgBootstrapBaseView]) -> None:
        assert call(view, "delete", {"organization_id": 409}).status_code == 409

    def test_a_missing_organization_is_a_400(self, view: type[OrgBootstrapBaseView]) -> None:
        assert call(view, "delete", {}).status_code == 400


class TestGuard:
    def test_an_absent_identity_is_a_401(self, view: type[OrgBootstrapBaseView]) -> None:
        # Since envoy-pyauth v2.0.0 this is a 401, not the implicit internal call five docstrings still describe.
        assert call(view, "post", {"organization_id": 7}, envoy=None).status_code == 401

    def test_a_tenant_identity_is_a_403(self, view: type[OrgBootstrapBaseView]) -> None:
        assert call(view, "post", {"organization_id": 7}, envoy=TENANT).status_code == 403

    def test_the_guard_covers_delete_too(self, view: type[OrgBootstrapBaseView]) -> None:
        assert call(view, "delete", {"organization_id": 7}, envoy=TENANT).status_code == 403


class TestRefusalLog:
    def test_the_catalog_qualifies_the_refusal(self, driver: Driver, caplog: pytest.LogCaptureFixture) -> None:
        built = build_org_bootstrap_view(
            clone=driver.clone, teardown=driver.teardown, error=ServiceCloneError, catalog="alarm-template"
        )
        with caplog.at_level(logging.WARNING):
            call(built, "post", {"organization_id": 409})
        assert "org-bootstrap refused for org 409 (alarm-template)" in caplog.text

    def test_without_a_catalog_the_original_line_is_kept(
        self, view: type[OrgBootstrapBaseView], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            call(view, "post", {"organization_id": 409})
        assert "org-bootstrap refused for org 409: " in caplog.text

    def test_delete_logs_the_refusal_the_six_copies_swallowed(
        self, view: type[OrgBootstrapBaseView], caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            call(view, "delete", {"organization_id": 409})
        assert "org-bootstrap refused for org 409" in caplog.text


class TestSubclassing:
    def test_a_subclass_can_add_an_action_without_redefining_post(
        self, view: type[OrgBootstrapBaseView], driver: Driver
    ) -> None:
        # asset-service adds a GET clone-map action on the same skeleton.
        from rest_framework.response import Response

        class CloneMapView(view):  # type: ignore[valid-type, misc]
            def get(self, request: Any) -> Response:
                return Response({"clone_map": {"1": 11}})

        factory = APIRequestFactory()
        request = factory.get("/api/internal/org-bootstrap/")
        request.envoy = PLATFORM
        assert CloneMapView.as_view()(request).data == {"clone_map": {"1": 11}}
        assert call(CloneMapView, "post", {"organization_id": 7}).status_code == 200


class TestSerializers:
    def test_bootstrap_defaults(self) -> None:
        serializer = OrgBootstrapSerializer(data={"organization_id": 3})
        assert serializer.is_valid()
        assert serializer.validated_data == {"organization_id": 3, "source_organization_id": 0, "run_id": ""}

    def test_teardown_defaults(self) -> None:
        serializer = OrgTeardownSerializer(data={"organization_id": 3})
        assert serializer.is_valid()
        assert serializer.validated_data == {"organization_id": 3, "run_id": ""}
