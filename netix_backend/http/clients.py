"""Factory for generated OpenAPI clients: env-derived config, shared retry transport, process-wide cache."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from functools import partial
from typing import Any

import httpx

from netix_backend.env import ConfigurationError, env_first, parse_bool, service_authorization
from netix_backend.http.retry import AsyncRetryTransport, RetryTransport

DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)
POOL_ACQUIRE_TIMEOUT = 5.0
DEFAULT_ACCEPT = "application/json"

_ENV_SUFFIXES = {
    "url": "URL",
    "accept": "HEADERS",
    "auth": "AUTH",
    "verify_ssl": "VERIFY_SSL",
    "raise_status": "RAISE_ON_UNEXPECTED_STATUS",
    "timeout": "TIMEOUT",
}

_client_cache: dict[tuple[Any, ...], Any] = {}
_client_cache_lock = threading.Lock()


def build_headers(
    accept: str = DEFAULT_ACCEPT,
    authorization: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Standard inter-service request headers."""
    headers = {"Accept": accept}
    if authorization:
        headers["Authorization"] = authorization
    if extra:
        headers.update(extra)
    return headers


def reset_client_cache() -> None:
    """Drop every cached client; only tests need this, to isolate environment changes."""
    with _client_cache_lock:
        _client_cache.clear()


def _keys(explicit: Sequence[str] | None, service: str, part: str) -> tuple[str, ...]:
    return tuple(explicit) if explicit else (f"{service.upper()}_SVC_{_ENV_SUFFIXES[part]}",)


def _required_float(keys: tuple[str, ...]) -> float:
    raw = env_first(*keys, required=True)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{keys[0]}={raw!r} is not a number") from exc


def _construct_client(
    client_cls: Any,
    *,
    keys: dict[str, tuple[str, ...]],
    authorization: bool,
    extra_headers: dict[str, str] | None,
    limits: httpx.Limits,
    pool_timeout: float,
    retries: int | None,
) -> Any:
    base_url = env_first(*keys["url"], required=True)
    accept = env_first(*keys["accept"], default=DEFAULT_ACCEPT)
    verify_ssl = parse_bool(env_first(*keys["verify_ssl"]))
    raise_on_unexpected_status = parse_bool(env_first(*keys["raise_status"]))
    timeout_seconds = _required_float(keys["timeout"])
    auth = service_authorization(*keys["auth"]) if authorization else None
    headers = build_headers(accept=accept, authorization=auth, extra=extra_headers)
    # Explicit pool timeout: a saturated shared pool must not make callers wait the full read timeout.
    timeout = httpx.Timeout(timeout_seconds, pool=pool_timeout)

    # One transport shared with the eager sync client below, instead of built and discarded.
    transport = RetryTransport(retries=retries, limits=limits, verify=verify_ssl)
    client = client_cls(
        base_url=base_url,
        headers=headers,
        verify_ssl=verify_ssl,
        raise_on_unexpected_status=raise_on_unexpected_status,
        timeout=timeout,
        follow_redirects=False,
        httpx_args={"transport": transport},
    )
    if hasattr(client, "set_httpx_client"):
        client.set_httpx_client(
            httpx.Client(
                base_url=base_url,
                headers=headers,
                timeout=timeout,
                verify=verify_ssl,
                follow_redirects=False,
                transport=transport,
            )
        )
    if hasattr(client, "set_async_httpx_client"):
        client.set_async_httpx_client(
            httpx.AsyncClient(
                base_url=base_url,
                headers=headers,
                timeout=timeout,
                verify=verify_ssl,
                follow_redirects=False,
                transport=AsyncRetryTransport(retries=retries, limits=limits, verify=verify_ssl),
            )
        )
    return client


def build_client(
    client_cls: Any,
    *,
    service: str,
    url_keys: Sequence[str] | None = None,
    accept_keys: Sequence[str] | None = None,
    auth_keys: Sequence[str] | None = None,
    verify_ssl_keys: Sequence[str] | None = None,
    raise_status_keys: Sequence[str] | None = None,
    timeout_keys: Sequence[str] | None = None,
    authorization: bool = True,
    extra_headers: dict[str, str] | None = None,
    cache: bool = True,
    limits: httpx.Limits = DEFAULT_LIMITS,
    pool_timeout: float = POOL_ACQUIRE_TIMEOUT,
    retries: int | None = None,
) -> Any:
    """Build (and by default cache) a generated client from the ``<SVC>_SVC_*`` env quintet, key tuples overriding."""
    keys = {
        part: _keys(explicit, service, part)
        for part, explicit in (
            ("url", url_keys),
            ("accept", accept_keys),
            ("auth", auth_keys),
            ("verify_ssl", verify_ssl_keys),
            ("raise_status", raise_status_keys),
            ("timeout", timeout_keys),
        )
    }
    build = partial(
        _construct_client,
        client_cls,
        keys=keys,
        authorization=authorization,
        extra_headers=extra_headers,
        limits=limits,
        pool_timeout=pool_timeout,
        retries=retries,
    )
    if not cache:
        return build()
    # The resolved keys are part of the identity: one client class is built twice with different credentials.
    cache_key = (
        f"{client_cls.__module__}.{client_cls.__qualname__}",
        service.upper(),
        authorization,
        tuple(sorted(keys.items())),
    )
    with _client_cache_lock:
        client = _client_cache.get(cache_key)
        if client is None:
            # httpx clients are thread-safe for concurrent requests, so one per process is sound.
            client = build()
            _client_cache[cache_key] = client
    return client


__all__ = (
    "DEFAULT_ACCEPT",
    "DEFAULT_LIMITS",
    "POOL_ACQUIRE_TIMEOUT",
    "build_client",
    "build_headers",
    "reset_client_cache",
)
