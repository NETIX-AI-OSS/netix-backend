"""Tests for the shared test-double kit: the response stub, the authenticated clients and the HTTP guard."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from django.core.cache import cache
from django.test import override_settings

from netix_backend.django import testing
from netix_backend.django.testing import ClientResponse, UnmockedHTTPCall, block_http, client_response, envoy_api_client
from tests.models import ScopedWidget
from tests.urls import WidgetViewSet

CAFM_IDENTITY = {"organization": 0, "is_superuser": True, "permissions": ["quotation-delete"]}
RESOLVER_MIDDLEWARE = ["django.middleware.common.CommonMiddleware", testing.ENVOY_MIDDLEWARE_PATH]


@pytest.fixture
def widget(db: Any) -> ScopedWidget:
    return ScopedWidget.objects.create(label="listed", organization_id=7)


class TestClientResponse:
    def test_the_defaults_match_the_stub_nine_repos_hand_roll(self) -> None:
        response = client_response()
        assert response.status_code == 200
        assert response.parsed is None
        assert response.content == b""
        assert response.text == ""
        assert response.results == []
        assert response.count == 0

    def test_every_field_is_settable(self) -> None:
        response = client_response(404, parsed={"detail": "nope"}, content=b"{}", text="{}")
        assert response.status_code == 404
        assert response.parsed == {"detail": "nope"}
        assert response.content == b"{}"
        assert response.text == "{}"

    def test_count_defaults_to_the_page_length(self) -> None:
        assert client_response(results=[{"id": 1}, {"id": 2}]).count == 2

    def test_an_explicit_count_wins_for_a_paginated_page(self) -> None:
        response = client_response(results=[{"id": 1}], count=97)
        assert response.count == 97

    def test_the_results_list_is_copied(self) -> None:
        rows = [{"id": 1}]
        response = client_response(results=rows)
        rows.append({"id": 2})
        assert response.results == [{"id": 1}]

    def test_extra_fields_are_kept_for_the_repos_that_stub_more(self) -> None:
        response = ClientResponse(status_code=200, headers={"X-Total": "3"})
        assert response.headers == {"X-Total": "3"}

    def test_the_repr_names_the_fields(self) -> None:
        assert "status_code" in repr(client_response())

    def test_the_factory_fixture_hands_back_the_helper(self, client_response_factory: Any) -> None:
        assert client_response_factory is client_response
        assert client_response_factory(201).status_code == 201


class TestEnvoyApiClient:
    def test_the_resolver_seam_authenticates_every_request(
        self, monkeypatch: pytest.MonkeyPatch, widget: ScopedWidget
    ) -> None:
        with override_settings(MIDDLEWARE=RESOLVER_MIDDLEWARE):
            client = envoy_api_client(monkeypatch)
            response = client.get("/api/widgets/")
        assert response.status_code == 200
        assert [row["label"] for row in response.json()["results"]] == ["listed"]

    def test_permissions_replace_the_minimal_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import envoy_pyauth.middleware as envoy_middleware

        envoy_api_client(monkeypatch, permissions=["gateway-view"])
        assert envoy_middleware._resolve("Bearer x")["permissions"] == ["gateway-view"]

    def test_no_permissions_leaves_the_identity_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import envoy_pyauth.middleware as envoy_middleware

        envoy_api_client(monkeypatch, identity=CAFM_IDENTITY)
        assert envoy_middleware._resolve("Bearer x") == CAFM_IDENTITY

    def test_the_default_bearer_is_the_configured_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = envoy_api_client(monkeypatch)
        assert client._credentials["HTTP_AUTHORIZATION"] == testing.DEFAULT_TEST_BEARER

    @override_settings(NETIX_TEST_ENVOY_BEARER="Bearer asset-service-test-platform")
    def test_the_settings_bearer_is_picked_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = envoy_api_client(monkeypatch)
        assert client._credentials["HTTP_AUTHORIZATION"] == "Bearer asset-service-test-platform"

    def test_an_explicit_bearer_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = envoy_api_client(monkeypatch, bearer="api haystack-key")
        assert client._credentials["HTTP_AUTHORIZATION"] == "api haystack-key"

    def test_handler_mode_stamps_the_request_without_a_resolver(
        self, monkeypatch: pytest.MonkeyPatch, widget: ScopedWidget
    ) -> None:
        # ml-engine's seam: tests/settings.py carries no Envoy middleware, so nothing overwrites the stamp.
        client = envoy_api_client(monkeypatch, mode="handler", identity=CAFM_IDENTITY)
        response = client.get("/api/widgets/")
        assert [row["label"] for row in response.json()["results"]] == ["listed"]

    def test_the_fixture_follows_the_overridable_identity(
        self, envoy_client: Any, netix_envoy_identity: dict[str, Any], widget: ScopedWidget
    ) -> None:
        import envoy_pyauth.middleware as envoy_middleware

        assert envoy_middleware._resolve("Bearer x") == netix_envoy_identity


class TestNetixEnvoyIdentity:
    def test_it_defaults_to_the_minimal_identity(self, netix_envoy_identity: dict[str, Any]) -> None:
        assert netix_envoy_identity == testing.DEFAULT_ENVOY_IDENTITY
        assert netix_envoy_identity is not testing.DEFAULT_ENVOY_IDENTITY

    @override_settings(NETIX_TEST_ENVOY_IDENTITY=CAFM_IDENTITY)
    def test_it_now_reads_the_same_setting_the_middlewares_read(self, request: pytest.FixtureRequest) -> None:
        # The fixture side and the settings side of a repo used to disagree; both now name one identity.
        # Resolved inside the body because override_settings only takes effect once the test function runs.
        identity = request.getfixturevalue("netix_envoy_identity")
        assert identity["permissions"] == ["quotation-delete"]


class TestBlockHttp:
    def test_a_sync_call_is_refused_with_an_actionable_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        block_http(monkeypatch)
        request = httpx.Request("GET", "http://user-management.backend:8001/auth/me/")
        with pytest.raises(UnmockedHTTPCall, match="GET http://user-management.backend:8001/auth/me/"):
            httpx.HTTPTransport().handle_request(request)

    def test_the_message_says_what_to_do(self, monkeypatch: pytest.MonkeyPatch) -> None:
        block_http(monkeypatch)
        with pytest.raises(UnmockedHTTPCall, match="MockTransport"):
            httpx.HTTPTransport().handle_request(httpx.Request("GET", "http://x.invalid/"))

    async def test_an_async_call_is_refused_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        block_http(monkeypatch)
        request = httpx.Request("POST", "http://static-service.backend/upload/")
        with pytest.raises(UnmockedHTTPCall, match="Unmocked async HTTP call"):
            await httpx.AsyncHTTPTransport().handle_async_request(request)

    def test_an_allowed_url_reaches_the_transport_underneath(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[str] = []

        def fake(transport: Any, request: Any) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, request=request)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fake)
        block_http(monkeypatch, allow=("allowed.invalid",))
        response = httpx.HTTPTransport().handle_request(httpx.Request("GET", "http://allowed.invalid/ping"))
        assert response.status_code == 200
        assert seen == ["http://allowed.invalid/ping"]

    async def test_an_allowed_url_reaches_the_async_transport_underneath(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake(transport: Any, request: Any) -> httpx.Response:
            return httpx.Response(204, request=request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake)
        block_http(monkeypatch, allow=("allowed.invalid",))
        request = httpx.Request("GET", "http://allowed.invalid/ping")
        response = await httpx.AsyncHTTPTransport().handle_async_request(request)
        assert response.status_code == 204

    def test_an_unlisted_host_is_still_refused_when_an_allowlist_exists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        block_http(monkeypatch, allow=("allowed.invalid",))
        with pytest.raises(UnmockedHTTPCall):
            httpx.HTTPTransport().handle_request(httpx.Request("GET", "http://other.invalid/ping"))

    def test_the_fixture_installs_the_guard(self, no_unmocked_http: None) -> None:
        with pytest.raises(UnmockedHTTPCall):
            httpx.HTTPTransport().handle_request(httpx.Request("GET", "http://x.invalid/"))

    def test_the_guard_is_lifted_again_afterwards(self) -> None:
        assert httpx.HTTPTransport.handle_request.__qualname__.startswith("HTTPTransport")


class TestClearEnvoyCache:
    @pytest.fixture
    def seeded_cache(self) -> str:
        key = "envoy_pyauth:auth:v2:deadbeef"
        cache.set(key, {"organization": 1, "permissions": []})
        return key

    def test_the_positive_auth_me_cache_is_dropped_on_entry(self, seeded_cache: str, clear_envoy_cache: None) -> None:
        # The exit clear runs when this fixture unwinds, so a rolled-back pk cannot leave a stale entry behind.
        assert cache.get(seeded_cache) is None


class TestEnvoyRequestFactory:
    def test_it_stamps_the_identity_middleware_would_attach(
        self, envoy_request_factory: Any, netix_envoy_identity: dict[str, Any]
    ) -> None:
        request = envoy_request_factory.get("/api/widgets/")
        assert request.envoy == netix_envoy_identity
        assert request.META["HTTP_AUTHORIZATION"] == testing.DEFAULT_TEST_BEARER

    @override_settings(NETIX_TEST_ENVOY_BEARER="Bearer cafm-backend-test-platform")
    def test_the_bearer_follows_the_setting(self, request: pytest.FixtureRequest) -> None:
        factory = request.getfixturevalue("envoy_request_factory")
        assert factory.get("/api/widgets/").META["HTTP_AUTHORIZATION"] == "Bearer cafm-backend-test-platform"

    def test_a_caller_supplied_credential_is_left_alone(self, envoy_request_factory: Any) -> None:
        request = envoy_request_factory.get("/api/widgets/", HTTP_AUTHORIZATION="Bearer someone-else")
        assert request.META["HTTP_AUTHORIZATION"] == "Bearer someone-else"

    def test_a_direct_view_call_is_scoped_without_middleware(
        self, envoy_request_factory: Any, widget: ScopedWidget
    ) -> None:
        view = WidgetViewSet.as_view({"get": "list"})
        response = view(envoy_request_factory.get("/api/widgets/"))
        assert response.status_code == 200
        assert [row["label"] for row in response.data["results"]] == ["listed"]

    def test_post_goes_through_the_same_stamping(self, envoy_request_factory: Any) -> None:
        assert envoy_request_factory.post("/api/widgets/", {"label": "x"}, format="json").envoy is not None
