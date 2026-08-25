"""The two test-only identity middlewares seven repos hand-roll, driven from settings instead of a local module."""

from __future__ import annotations

from typing import Any

import pytest
from django.test import RequestFactory, override_settings
from rest_framework.test import APIClient

from netix_backend.django import testing
from netix_backend.django.testing_middleware import (
    EnvoyTestAuthorizationMiddleware,
    ExplicitEnvoyIdentityMiddleware,
    configured_bearer,
    configured_identity,
)
from tests.models import ScopedWidget

CAFM_IDENTITY = {"organization": 0, "is_superuser": True, "permissions": ["quotation-delete"]}


def view(request: Any) -> None:
    """A stand-in for the resolved view function the middleware is handed."""


def make_request(**meta: str) -> Any:
    return RequestFactory().get("/api/widgets/", **meta)


class TestConfiguredIdentity:
    def test_defaults_to_the_minimal_identity(self) -> None:
        assert configured_identity() == testing.DEFAULT_ENVOY_IDENTITY

    @override_settings(NETIX_TEST_ENVOY_IDENTITY=CAFM_IDENTITY)
    def test_reads_the_setting(self) -> None:
        assert configured_identity()["permissions"] == ["quotation-delete"]

    @override_settings(NETIX_TEST_ENVOY_IDENTITY=CAFM_IDENTITY)
    def test_returns_an_isolated_copy(self) -> None:
        configured_identity()["permissions"].append("stowaway")
        assert CAFM_IDENTITY["permissions"] == ["quotation-delete"]

    @override_settings(NETIX_TEST_ENVOY_IDENTITY={"organization": 3})
    def test_an_identity_without_permissions_still_gets_a_list(self) -> None:
        assert configured_identity() == {"organization": 3, "permissions": []}

    def test_the_bearer_defaults_to_the_library_token(self) -> None:
        assert configured_bearer() == testing.DEFAULT_TEST_BEARER

    @override_settings(NETIX_TEST_ENVOY_BEARER="Bearer asset-service-test-platform")
    def test_the_bearer_is_per_repo(self) -> None:
        assert configured_bearer() == "Bearer asset-service-test-platform"


class TestEnvoyTestAuthorizationMiddleware:
    @override_settings(NETIX_TEST_ENVOY_IDENTITY=CAFM_IDENTITY)
    def test_injects_the_token_and_resolves_it_at_the_v2_seam(self) -> None:
        request = make_request()
        middleware = EnvoyTestAuthorizationMiddleware(lambda _request: None)
        assert middleware.process_view(request, view, (), {}) is None
        assert request.META["HTTP_AUTHORIZATION"] == testing.DEFAULT_TEST_BEARER
        assert request.envoy == CAFM_IDENTITY

    def test_a_request_that_brings_its_own_credential_is_resolved_for_real(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # update-service and stormbreaker use META.setdefault with no guard, so their denial tests pass vacuously.
        import envoy_pyauth.middleware as envoy_middleware

        monkeypatch.setattr(envoy_middleware, "_resolve", lambda header: {"organization": 7, "permissions": []})
        request = make_request(HTTP_AUTHORIZATION="Bearer someone-elses-token")
        EnvoyTestAuthorizationMiddleware(lambda _request: None).process_view(request, view, (), {})
        assert request.envoy == {"organization": 7, "permissions": []}
        assert request.META["HTTP_AUTHORIZATION"] == "Bearer someone-elses-token"

    def test_the_guard_can_be_turned_off_for_the_two_repos_that_never_had_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import envoy_pyauth.middleware as envoy_middleware

        monkeypatch.setattr(envoy_middleware, "_resolve", lambda header: {"organization": 7, "permissions": []})

        class UnguardedMiddleware(EnvoyTestAuthorizationMiddleware):
            header_guard = False

        request = make_request(HTTP_AUTHORIZATION="Bearer someone-elses-token")
        UnguardedMiddleware(lambda _request: None).process_view(request, view, (), {})
        assert request.envoy == testing.DEFAULT_ENVOY_IDENTITY


class TestExplicitEnvoyIdentityMiddleware:
    @override_settings(NETIX_TEST_ENVOY_IDENTITY=CAFM_IDENTITY)
    def test_assigns_the_identity_without_touching_the_resolver(self) -> None:
        request = make_request()
        assert ExplicitEnvoyIdentityMiddleware(lambda _request: None).process_view(request, view, (), {}) is None
        assert request.envoy == CAFM_IDENTITY
        assert "HTTP_AUTHORIZATION" not in request.META


class TestThroughTheRealStack:
    @pytest.fixture
    def widget(self, db: Any) -> ScopedWidget:
        return ScopedWidget.objects.create(label="listed", organization_id=7)

    def test_an_unidentified_request_reaches_no_rows(self, widget: ScopedWidget) -> None:
        assert APIClient().get("/api/widgets/").json()["results"] == []

    def test_the_swapped_middleware_authenticates_every_client_request(self, widget: ScopedWidget) -> None:
        middleware = testing.swap_auth_middleware(
            ["django.middleware.common.CommonMiddleware", testing.ENVOY_MIDDLEWARE_PATH],
            testing.EXPLICIT_MIDDLEWARE_PATH,
        )
        with override_settings(MIDDLEWARE=middleware, NETIX_TEST_ENVOY_IDENTITY=CAFM_IDENTITY):
            response = APIClient().get("/api/widgets/")
        assert response.status_code == 200
        assert [row["label"] for row in response.json()["results"]] == ["listed"]
