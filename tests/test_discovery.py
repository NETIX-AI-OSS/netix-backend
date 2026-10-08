"""Registry discovery: the derived URL, the per-process cache, outage tolerance and the env fallback."""

import io
import json

import pytest

from netix_backend import discovery

ENVIRONMENT = {
    "UPDATE_SERVICE_REGISTRATION_URL": "http://update-service.backend:8000/api/registry/registration/",
    "SERVICE_REGISTRATION_TOKEN": "test-registration-token",
    "SERVICE_REGISTRATION_ENVIRONMENT": "staging",
    "ASSET_SVC_URL": "http://asset-from-configmap:8000",
}
REGISTRY = {
    "services": [
        {"name": "asset-service", "internal_base_url": "http://asset-service.backend:8000"},
        {"name": "half-registered", "internal_base_url": None},
    ]
}


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    discovery.reset_discovery_cache()
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    yield
    discovery.reset_discovery_cache()


@pytest.fixture
def registry(monkeypatch):
    requests = []

    def serve(request, timeout):
        requests.append((request, timeout))
        return io.BytesIO(json.dumps(REGISTRY).encode())

    monkeypatch.setattr(discovery, "urlopen", serve)
    return requests


def test_the_discovery_url_is_the_registration_routes_sibling(monkeypatch):
    assert discovery.discovery_url() == "http://update-service.backend:8000/api/registry/discovery/"
    monkeypatch.delenv("UPDATE_SERVICE_REGISTRATION_URL")
    assert discovery.discovery_url() is None


def test_a_registered_service_wins_over_its_configmap_variable(registry):
    assert discovery.service_url("asset-service", "ASSET_SVC_URL") == "http://asset-service.backend:8000"

    request, timeout = registry[0]
    assert request.full_url == "http://update-service.backend:8000/api/registry/discovery/?environment=staging"
    assert request.get_header("X-service-registration-token") == "test-registration-token"
    assert timeout == discovery.TIMEOUT_SECONDS


def test_an_unregistered_service_falls_back_to_the_env(registry):
    assert discovery.service_url("half-registered", "ASSET_SVC_URL") == "http://asset-from-configmap:8000"
    assert discovery.service_url("half-registered") is None
    assert discovery.discovered_urls() == {"asset-service": "http://asset-service.backend:8000"}


def test_the_registry_is_fetched_once_per_cache_window(monkeypatch, registry):
    clock = iter([0.0, 1.0, 1.0, discovery.CACHE_SECONDS + 1, discovery.CACHE_SECONDS + 1])
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(clock))

    discovery.discovered_urls()
    discovery.discovered_urls()
    discovery.discovered_urls()

    assert len(registry) == 2


def test_a_failed_fetch_keeps_the_last_known_urls(monkeypatch, registry, caplog):
    clock = iter([0.0, 0.0, discovery.CACHE_SECONDS, discovery.CACHE_SECONDS])
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(clock))
    discovery.discovered_urls()

    def down(*_args, **_kwargs):
        raise OSError("update-service down")

    monkeypatch.setattr(discovery, "urlopen", down)

    with caplog.at_level("WARNING"):
        assert discovery.service_url("asset-service") == "http://asset-service.backend:8000"
    assert "Service discovery failed" in caplog.text


def test_an_unconfigured_service_never_calls_update_service(monkeypatch):
    monkeypatch.delenv("SERVICE_REGISTRATION_TOKEN")
    monkeypatch.setattr(discovery, "urlopen", lambda *_args, **_kwargs: pytest.fail("must not call update-service"))
    assert discovery.service_url("asset-service", "ASSET_SVC_URL") == "http://asset-from-configmap:8000"
