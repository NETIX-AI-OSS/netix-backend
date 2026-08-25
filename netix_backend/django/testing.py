"""Pytest plugin: shared fixtures for NETIX backend test suites."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Final, Literal

import pytest

# Loaded as a pytest11 plugin in every consumer repo, so module scope stays free of django and envoy imports.

if TYPE_CHECKING:
    from rest_framework.test import APIClient, APIRequestFactory

DEFAULT_ENVOY_IDENTITY: Final[dict[str, Any]] = {
    "organization": 0,
    "is_superuser": True,
    "permissions": [],
}

# Opt-in superset: the five resolver-seam repos carry all of it, cafm and visualization-backend carry less.
DEFAULT_TEST_PLATFORM_IDENTITY: Final[dict[str, Any]] = {
    "username": "platform_internal",
    "user_id": 0,
    "organization": 0,
    "is_superuser": True,
    "is_platform_internal": True,
    "user_type": "service",
    "permissions": [],
    "groups": [],
    "feature_flags": [],
}

DEFAULT_TEST_BEARER: Final[str] = "Bearer netix-backend-test-platform"
ENVOY_MIDDLEWARE_PATH: Final[str] = "envoy_pyauth.middleware.AuthorizationMiddleware"
TEST_MIDDLEWARE_PATH: Final[str] = "netix_backend.django.testing.EnvoyTestAuthorizationMiddleware"
EXPLICIT_MIDDLEWARE_PATH: Final[str] = "netix_backend.django.testing.ExplicitEnvoyIdentityMiddleware"

# Django's MIDDLEWARE takes dotted paths only, so the two classes are resolved on first attribute access.
_LAZY_MIDDLEWARE: Final[frozenset[str]] = frozenset(
    {"EnvoyTestAuthorizationMiddleware", "ExplicitEnvoyIdentityMiddleware"}
)

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
    "CSRF_TRUSTED_ORIGINS": "http://localhost:8000",
}


# Superseded by test_settings.apply_test_env(), which applies the mapping instead of returning it.
def netix_test_settings(*, extra_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The env defaults every app/settings_test.py duplicates; apply with os.environ.setdefault."""
    env = dict(TEST_ENV_DEFAULTS)
    if extra_env:
        env.update(extra_env)
    return env


# Superseded by test_settings.test_overrides(database=...), which also covers aliases, mirrors and engines.
def sqlite_databases(name: str = ":memory:") -> dict[str, Any]:
    """A DATABASES dict pointing at a SQLite file (or the default in-memory database)."""
    from netix_backend.django.test_settings import test_overrides

    return dict(test_overrides(database=name)["DATABASES"])


def platform_test_identity(*, permissions: Sequence[str] = (), **overrides: Any) -> dict[str, Any]:
    """An isolated copy of the platform identity; services supply their own permission codenames."""
    identity = {**DEFAULT_TEST_PLATFORM_IDENTITY, "permissions": list(permissions)}
    identity.update(overrides)
    return identity


def swap_auth_middleware(
    middleware: Iterable[str],
    replacement: str = TEST_MIDDLEWARE_PATH,
    *,
    target: str = ENVOY_MIDDLEWARE_PATH,
) -> list[str]:
    """The list comprehension six test settings modules copy, swapping the Envoy middleware for a test-only one."""
    return [replacement if entry == target else entry for entry in middleware]


def assert_timeout_invariant(*, request_timeout_seconds: float, statement_timeout_ms: int) -> None:
    """A DB-bound request must surface its own statement_timeout error rather than an opaque 504."""
    if request_timeout_seconds > statement_timeout_ms / 1000:
        return
    raise AssertionError(
        f"REQUEST_TIMEOUT_SECONDS ({request_timeout_seconds}) must exceed "
        f"DB_STATEMENT_TIMEOUT_MS / 1000 ({statement_timeout_ms / 1000})"
    )


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


class UnmockedHTTPCall(RuntimeError):
    """Raised when a test reaches httpx' real transport instead of a mock."""


class ClientResponse:
    """The generated-client response stub nine repos hand-roll; extra keyword fields are kept as attributes."""

    status_code: int
    parsed: Any
    content: bytes
    text: str
    results: list[Any]
    count: int

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)

    def __repr__(self) -> str:
        return f"ClientResponse({self.__dict__})"


def client_response(
    status_code: int = 200,
    *,
    parsed: Any = None,
    content: bytes = b"",
    text: str = "",
    results: Sequence[Any] | None = None,
    count: int | None = None,
) -> ClientResponse:
    """An openapi-python-client Response stand-in; ``count`` defaults to the length of ``results``."""
    rows = [] if results is None else list(results)
    return ClientResponse(
        status_code=status_code,
        parsed=parsed,
        content=content,
        text=text,
        results=rows,
        count=len(rows) if count is None else count,
    )


def envoy_api_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity: Mapping[str, Any] | None = None,
    permissions: Sequence[str] = (),
    bearer: str | None = None,
    mode: Literal["resolver", "handler"] = "resolver",
) -> APIClient:
    """An authenticated APIClient, written six ways across six repos: resolve a test bearer, or stamp the request.

    ``mode="handler"`` wraps the test client's own handler instead of the resolver seam, for suites whose
    MIDDLEWARE carries no Envoy middleware at all; anything in MIDDLEWARE would overwrite the stamp.
    """
    from rest_framework.test import APIClient

    from netix_backend.django.testing_middleware import configured_bearer

    resolved = dict(DEFAULT_ENVOY_IDENTITY if identity is None else identity)
    if permissions:
        resolved["permissions"] = list(permissions)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=configured_bearer() if bearer is None else bearer)
    if mode == "handler":
        original_get_response = client.handler.get_response

        def get_response(request: Any) -> Any:
            request.envoy = dict(resolved)
            return original_get_response(request)

        monkeypatch.setattr(client.handler, "get_response", get_response)
        return client
    monkeypatch.setattr("envoy_pyauth.middleware._resolve", lambda _header: dict(resolved))
    return client


def block_http(monkeypatch: pytest.MonkeyPatch, *, allow: Sequence[str] = ()) -> None:
    """Fail fast on an unmocked httpx call rather than silently retrying against a real host.

    ``allow`` holds substrings of the request URL that are let through to the real transport.
    """
    import httpx

    original_sync = httpx.HTTPTransport.handle_request
    original_async = httpx.AsyncHTTPTransport.handle_async_request
    allowed = tuple(allow)

    def permitted(request: Any) -> bool:
        return any(pattern in str(request.url) for pattern in allowed)

    def message(request: Any, kind: str) -> str:
        return (
            f"Unmocked {kind}HTTP call in tests: {request.method} {request.url}. "
            "Patch the service method, install an httpx MockTransport, or pass allow=(...) to block_http()."
        )

    def handle_request(transport: Any, request: Any) -> Any:
        if permitted(request):
            return original_sync(transport, request)
        raise UnmockedHTTPCall(message(request, ""))

    async def handle_async_request(transport: Any, request: Any) -> Any:
        if permitted(request):
            return await original_async(transport, request)
        raise UnmockedHTTPCall(message(request, "async "))

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle_async_request)


@pytest.fixture
def unscoped_envoy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passthrough: EnvoyQueryFilter stops scoping, for tests that only exercise view logic."""
    patch_envoy_unscoped(monkeypatch)


@pytest.fixture
def netix_envoy_identity() -> dict[str, Any]:
    """The identity the envoy fixtures inject: NETIX_TEST_ENVOY_IDENTITY, or the minimal default."""
    from netix_backend.django.testing_middleware import configured_identity

    return configured_identity()


@pytest.fixture
def explicit_envoy_identity(monkeypatch: pytest.MonkeyPatch, netix_envoy_identity: Mapping[str, Any]) -> None:
    """Direct view tests receive the identity middleware would attach in production."""
    patch_envoy_identity(monkeypatch, netix_envoy_identity)


@pytest.fixture
def client_response_factory() -> Any:
    """notification-service's ``dummy_response``: build a generated-client response stub per call."""
    return client_response


@pytest.fixture
def envoy_client(monkeypatch: pytest.MonkeyPatch, netix_envoy_identity: Mapping[str, Any]) -> APIClient:
    """An APIClient whose every request resolves to ``netix_envoy_identity``; override that fixture to change caller."""
    return envoy_api_client(monkeypatch, identity=netix_envoy_identity)


@pytest.fixture
def no_unmocked_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not autouse: request it explicitly, or re-declare it autouse in the repo conftest that wants it everywhere."""
    block_http(monkeypatch)


@pytest.fixture
def clear_envoy_cache() -> Iterator[None]:
    """Drop envoy-pyauth's positive /auth/me/ cache around the test; not autouse, because it clears every alias."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def envoy_request_factory(netix_envoy_identity: Mapping[str, Any]) -> APIRequestFactory:
    """APIRequestFactory bypasses middleware, so this one stamps the identity and bearer middleware would attach."""
    from rest_framework.test import APIRequestFactory

    from netix_backend.django.testing_middleware import configured_bearer

    identity = dict(netix_envoy_identity)
    bearer = configured_bearer()

    class EnvoyAPIRequestFactory(APIRequestFactory):
        def request(self, **kwargs: Any) -> Any:
            request: Any = super().request(**kwargs)
            request.META.setdefault("HTTP_AUTHORIZATION", bearer)
            request.envoy = dict(identity)
            return request

    return EnvoyAPIRequestFactory()


def __getattr__(name: str) -> Any:
    # Both middlewares import django and envoy, which must not happen while pytest is still loading plugins.
    if name in _LAZY_MIDDLEWARE:
        from netix_backend.django import testing_middleware

        value = getattr(testing_middleware, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
