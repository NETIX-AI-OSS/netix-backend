"""Generated-client factory: env quintet derivation, tuple overrides, caching and the shared transport."""

import httpx
import pytest

from netix_backend.env import ConfigurationError
from netix_backend.http import clients
from netix_backend.http.retry import AsyncRetryTransport, RetryTransport

QUINTET = {
    "ASSET_SVC_URL": "https://asset.internal",
    "ASSET_SVC_HEADERS": "application/json",
    "ASSET_SVC_AUTH": "asset-token",
    "ASSET_SVC_VERIFY_SSL": "1",
    "ASSET_SVC_RAISE_ON_UNEXPECTED_STATUS": "0",
    "ASSET_SVC_TIMEOUT": "12",
}


class FakeClient:
    """Stands in for a generated OpenAPI client without the ``set_*_httpx_client`` hooks."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeClientWithHooks(FakeClient):
    """Stands in for a generated client that accepts eagerly-built httpx clients."""

    sync_client: httpx.Client
    async_client: httpx.AsyncClient

    def set_httpx_client(self, client):
        self.sync_client = client

    def set_async_httpx_client(self, client):
        self.async_client = client


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    clients.reset_client_cache()
    for key, value in QUINTET.items():
        monkeypatch.setenv(key, value)
    yield
    clients.reset_client_cache()


def test_build_client_derives_the_env_quintet_from_the_service_name():
    client = clients.build_client(FakeClient, service="asset")
    assert client.kwargs["base_url"] == "https://asset.internal"
    assert client.kwargs["headers"] == {"Accept": "application/json", "Authorization": "asset-token"}
    assert client.kwargs["verify_ssl"] is True
    assert client.kwargs["raise_on_unexpected_status"] is False
    assert client.kwargs["follow_redirects"] is False
    assert client.kwargs["timeout"] == httpx.Timeout(12.0, pool=clients.POOL_ACQUIRE_TIMEOUT)
    assert isinstance(client.kwargs["httpx_args"]["transport"], RetryTransport)


def test_build_client_accepts_explicit_key_tuples_first_wins(monkeypatch):
    monkeypatch.setenv("USER_SVC_URL", "https://user.internal")
    monkeypatch.delenv("USER_SVC_TIMEOUT", raising=False)
    client = clients.build_client(
        FakeClient,
        service="USER",
        url_keys=("USER_SVC_URL",),
        auth_keys=("CAFM_SVC_AUTH", "ASSET_SVC_AUTH"),
        verify_ssl_keys=("CAFM_SVC_VERIFY_SSL", "ASSET_SVC_VERIFY_SSL"),
        timeout_keys=("CAFM_SVC_TIMEOUT", "ASSET_SVC_TIMEOUT"),
        extra_headers={"X-Netix": "1"},
    )
    assert client.kwargs["base_url"] == "https://user.internal"
    assert client.kwargs["headers"]["Authorization"] == "asset-token"
    assert client.kwargs["headers"]["X-Netix"] == "1"
    assert client.kwargs["verify_ssl"] is True


def test_build_client_defaults_accept_when_the_headers_key_is_absent(monkeypatch):
    monkeypatch.delenv("ASSET_SVC_HEADERS")
    client = clients.build_client(FakeClient, service="ASSET")
    assert client.kwargs["headers"]["Accept"] == clients.DEFAULT_ACCEPT


def test_build_client_without_authorization_sends_no_credential(monkeypatch):
    monkeypatch.delenv("ASSET_SVC_AUTH")
    monkeypatch.delenv("USER_SVC_AUTH", raising=False)
    client = clients.build_client(FakeClient, service="ASSET", authorization=False)
    assert "Authorization" not in client.kwargs["headers"]


def test_build_client_surfaces_a_named_error_for_a_missing_url(monkeypatch):
    monkeypatch.delenv("DATA_SVC_URL", raising=False)
    with pytest.raises(ConfigurationError, match="DATA_SVC_URL"):
        clients.build_client(FakeClient, service="DATA")


def test_build_client_requires_a_timeout(monkeypatch):
    monkeypatch.delenv("ASSET_SVC_TIMEOUT")
    with pytest.raises(ConfigurationError, match="ASSET_SVC_TIMEOUT"):
        clients.build_client(FakeClient, service="ASSET")


def test_build_client_rejects_an_unparseable_timeout(monkeypatch):
    monkeypatch.setenv("ASSET_SVC_TIMEOUT", "soon")
    with pytest.raises(ConfigurationError, match="not a number"):
        clients.build_client(FakeClient, service="ASSET")


def test_build_client_caches_per_class_service_and_authorization():
    first = clients.build_client(FakeClient, service="ASSET")
    assert clients.build_client(FakeClient, service="ASSET") is first
    assert clients.build_client(FakeClient, service="ASSET", authorization=False) is not first
    clients.reset_client_cache()
    assert clients.build_client(FakeClient, service="ASSET") is not first


def test_build_client_can_skip_the_cache():
    first = clients.build_client(FakeClient, service="ASSET", cache=False)
    assert clients.build_client(FakeClient, service="ASSET", cache=False) is not first


def test_build_client_shares_one_transport_with_the_eager_clients():
    client = clients.build_client(FakeClientWithHooks, service="ASSET", retries=0)
    transport = client.kwargs["httpx_args"]["transport"]
    try:
        assert client.sync_client._transport is transport
        assert transport.retries == 0
        assert isinstance(client.async_client._transport, AsyncRetryTransport)
        assert client.sync_client.headers["Authorization"] == "asset-token"
    finally:
        client.sync_client.close()


def test_build_headers_variants():
    assert clients.build_headers() == {"Accept": clients.DEFAULT_ACCEPT}
    assert clients.build_headers("text/csv", "token", {"X-A": "b"}) == {
        "Accept": "text/csv",
        "Authorization": "token",
        "X-A": "b",
    }


def test_the_cache_key_covers_the_resolved_env_keys(monkeypatch):
    """ml-engine reads one service's URL with another's credential; the cache must not hand back the wrong one."""
    monkeypatch.setenv("CAFM_SVC_AUTH", "cafm-token")
    default = clients.build_client(FakeClient, service="asset")
    borrowed = clients.build_client(FakeClient, service="asset", auth_keys=("CAFM_SVC_AUTH",))
    assert default.kwargs["headers"]["Authorization"] == "asset-token"
    assert borrowed.kwargs["headers"]["Authorization"] == "cafm-token"
    assert clients.build_client(FakeClient, service="asset") is default
