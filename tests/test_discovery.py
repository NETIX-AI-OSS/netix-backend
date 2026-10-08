"""Registry discovery: the derived URL, frontend origins, the background-refreshed cache, outages, env fallback."""

import io
import json
from types import SimpleNamespace

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
        {
            "name": "asset-service",
            "kind": "backend",
            "base_url": "https://asset.api.example.com",
            "internal_base_url": "http://asset-service.backend:8000",
        },
        {"name": "half-registered", "kind": "backend", "base_url": None, "internal_base_url": None},
        {
            "name": "cafm-v2-ui",
            "kind": "frontend",
            "base_url": "https://cafm.example.com/",
            "internal_base_url": "http://cafm-v2-ui.frontend:8080",
        },
        {"name": "pinned-ui", "kind": "frontend", "base_url": None, "internal_base_url": "http://pinned.frontend:80"},
    ]
}


class _InlineThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


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
    assert set(discovery.discovered_urls()) == {"asset-service", "cafm-v2-ui", "pinned-ui"}


def test_frontend_origins_are_the_public_origins_of_registered_frontends(registry):
    assert discovery.frontend_origins() == frozenset({"https://cafm.example.com"})


def test_only_the_first_lookup_fetches_inline_and_later_ones_refresh_in_the_background(monkeypatch, registry):
    clock = iter([0.0, 1.0, discovery.CACHE_SECONDS + 1])
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(clock))
    started = []
    monkeypatch.setattr(
        discovery,
        "threading",
        SimpleNamespace(Thread=lambda **kwargs: started.append(kwargs) or _InlineThread(**kwargs)),
    )

    discovery.discovered_urls()
    discovery.discovered_urls()
    assert (len(registry), len(started)) == (1, 0)

    discovery.discovered_urls()
    assert (len(registry), len(started)) == (2, 1)
    assert started[0]["daemon"] is True


def test_a_failed_refresh_keeps_the_last_known_urls(monkeypatch, registry, caplog):
    clock = iter([0.0, discovery.CACHE_SECONDS, discovery.CACHE_SECONDS + 1])
    monkeypatch.setattr(discovery.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(discovery, "threading", SimpleNamespace(Thread=_InlineThread))
    discovery.discovered_urls()

    def down(*_args, **_kwargs):
        raise OSError("update-service down")

    monkeypatch.setattr(discovery, "urlopen", down)
    with caplog.at_level("WARNING"):
        assert discovery.service_url("asset-service") == "http://asset-service.backend:8000"
    assert "Service discovery failed" in caplog.text
    assert discovery.frontend_origins() == frozenset({"https://cafm.example.com"})


def test_an_unconfigured_service_never_calls_update_service(monkeypatch):
    monkeypatch.delenv("SERVICE_REGISTRATION_TOKEN")
    monkeypatch.setattr(discovery, "urlopen", lambda *_args, **_kwargs: pytest.fail("must not call update-service"))
    assert discovery.service_url("asset-service", "ASSET_SVC_URL") == "http://asset-from-configmap:8000"
