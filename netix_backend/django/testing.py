"""Pytest plugin: shared fixtures for NETIX backend test suites."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Final

import pytest

# Loaded as a pytest11 plugin in every consumer repo, so module scope stays free of django and envoy imports.

DEFAULT_ENVOY_IDENTITY: Final[dict[str, Any]] = {
    "organization": 0,
    "is_superuser": True,
    "permissions": [],
}

SQLITE_MEMORY_DATABASES: Final[dict[str, Any]] = {
    "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
}

LOCMEM_CACHES: Final[dict[str, Any]] = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

TEST_ENV_DEFAULTS: Final[dict[str, str]] = {
    "DJANGO_SECRET": "test-secret-key-not-for-production",
    "DJANGO_DEBUG": "TRUE",
    "POSTGRES_NAME": "test",
    "POSTGRES_USER": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "SENTINEL_ENABLED": "FALSE",
    "REDIS_HOST": "localhost",
    "REDIS_PORT": "6379",
    "REDIS_DB": "0",
    "REDIS_PASSWORD": "",
    "CACHE_ENABLED": "FALSE",
    "SENTRY_ENABLED": "FALSE",
    "SESSION_CUSTOMER_FILTER": "FALSE",
}


def netix_test_settings(*, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env defaults every app/settings_test.py duplicates; apply with os.environ.setdefault."""
    env = dict(TEST_ENV_DEFAULTS)
    if extra_env:
        env.update(extra_env)
    return env


def sqlite_databases(name: str = ":memory:") -> dict[str, Any]:
    """A DATABASES dict pointing at a SQLite file (or the default in-memory database)."""
    return {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": name}}


def patch_envoy_unscoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable Envoy org scoping by patching the DEFINING module, whatever path consumers import it by."""
    from envoy_pyauth import utils as envoy_utils

    monkeypatch.setattr(
        envoy_utils.EnvoyQueryFilter,
        "filter_queryset",
        staticmethod(lambda request, queryset, *args, **kwargs: queryset),
    )
    monkeypatch.setattr(
        envoy_utils.EnvoyQueryFilter,
        "get_queryset",
        staticmethod(lambda request, model, *args, **kwargs: model.objects.all()),
    )


def patch_envoy_identity(monkeypatch: pytest.MonkeyPatch, identity: Mapping[str, Any] | None = None) -> None:
    """Keep real org scoping but hand it the identity middleware would have attached."""
    from envoy_pyauth import utils as envoy_utils

    envoy_filter = envoy_utils.EnvoyQueryFilter
    # Capture the UNBOUND originals so the substitutes can delegate to the real implementation.
    original_get_queryset = envoy_filter.get_queryset.__func__
    original_filter_queryset = envoy_filter.filter_queryset.__func__
    envoy_identity = dict(DEFAULT_ENVOY_IDENTITY if identity is None else identity)

    class _PlatformRequest:
        envoy = envoy_identity

    def _explicit_request(request: Any) -> Any:
        if request is None or not hasattr(request, "envoy"):
            return _PlatformRequest()
        return request

    def get_queryset(request: Any, *args: Any, **kwargs: Any) -> Any:
        return original_get_queryset(envoy_filter, _explicit_request(request), *args, **kwargs)

    def filter_queryset(request: Any, *args: Any, **kwargs: Any) -> Any:
        return original_filter_queryset(envoy_filter, _explicit_request(request), *args, **kwargs)

    monkeypatch.setattr(envoy_filter, "get_queryset", staticmethod(get_queryset))
    monkeypatch.setattr(envoy_filter, "filter_queryset", staticmethod(filter_queryset))


@contextmanager
def scoped_envoy(identity: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Context-manager form of the delegating identity patch, for non-fixture call sites."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        patch_envoy_identity(monkeypatch, identity)
        yield


@pytest.fixture
def unscoped_envoy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passthrough: EnvoyQueryFilter stops scoping, for tests that only exercise view logic."""
    patch_envoy_unscoped(monkeypatch)


@pytest.fixture
def netix_envoy_identity() -> dict[str, Any]:
    """The identity explicit_envoy_identity injects; override in a repo conftest to change it."""
    return dict(DEFAULT_ENVOY_IDENTITY)


@pytest.fixture
def explicit_envoy_identity(monkeypatch: pytest.MonkeyPatch, netix_envoy_identity: Mapping[str, Any]) -> None:
    """Direct view tests receive the identity middleware would attach in production."""
    patch_envoy_identity(monkeypatch, netix_envoy_identity)
