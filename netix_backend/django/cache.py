"""The ``SENTINEL_ENABLED`` CACHES block 13 settings.py copy verbatim, as one factory returning a frozen result."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from django.core.exceptions import ImproperlyConfigured

from netix_backend.env import ConfigurationError, env_bool, env_int

__all__ = (
    "DEFAULT_CLIENT",
    "DEFAULT_CONNECTION_FACTORY",
    "DEFAULT_REDIS_LOGGER",
    "REDIS_CACHE_BACKEND",
    "SENTINEL_CLIENT",
    "SENTINEL_CONNECTION_FACTORY",
    "SENTINEL_CONNECTION_POOL",
    "CacheTtls",
    "RedisCacheSettings",
    "cache_ttls",
    "redis_caches",
)

REDIS_CACHE_BACKEND: Final[str] = "django_redis.cache.RedisCache"
SENTINEL_CONNECTION_FACTORY: Final[str] = "django_redis.pool.SentinelConnectionFactory"
# django_redis' own default; bound unconditionally so a service never has to guard the else branch.
DEFAULT_CONNECTION_FACTORY: Final[str] = "django_redis.pool.ConnectionFactory"
SENTINEL_CLIENT: Final[str] = "django_redis.client.SentinelClient"
DEFAULT_CLIENT: Final[str] = "django_redis.client.DefaultClient"
SENTINEL_CONNECTION_POOL: Final[str] = "redis.sentinel.SentinelConnectionPool"
DEFAULT_REDIS_LOGGER: Final[str] = "django_redis.cache"


@dataclass(frozen=True)
class RedisCacheSettings:
    """What settings.py binds by name; never merged into ``globals()``, so prospector and mypy still see it."""

    caches: dict[str, Any]
    sentinels: list[tuple[str, str]]
    sentinel_hosts: list[str]
    connection_factory: str
    ignore_exceptions: bool
    log_ignored_exceptions: bool
    logger_name: str


@dataclass(frozen=True)
class CacheTtls:
    """CACHE_TTL and its two optional companions; five services have no CACHE_LONG_TTL to bind."""

    ttl: int
    long_ttl: int | None
    enabled: bool


def _env(name: str, *, strict: bool, fallback: str) -> str:
    """Exact ``os.environ`` semantics, empty strings included: strict raises, lenient falls back."""
    if not strict:
        return os.environ.get(name, fallback)
    try:
        return os.environ[name]
    except KeyError as exc:
        raise ConfigurationError(f"Missing required environment variable {name}") from exc


def _value(explicit: str | int | None, name: str, *, strict: bool, fallback: str) -> str:
    """An explicit argument wins; otherwise read the variable the ConfigMaps already carry."""
    if explicit is not None:
        return str(explicit)
    return _env(name, strict=strict, fallback=fallback)


def _resolve_sentinel_hosts(sentinel_hosts: Sequence[str] | str | None, *, strict: bool) -> list[str]:
    """Single ``SENTINEL_HOST`` unless the caller opts into a list; a comma-separated string is split."""
    raw: Sequence[str] | str = (
        _env("SENTINEL_HOST", strict=strict, fallback="localhost") if sentinel_hosts is None else sentinel_hosts
    )
    hosts = raw.split(",") if isinstance(raw, str) else list(raw)
    return [host.strip() for host in hosts if host.strip()]


def _sentinel_enabled(*, strict: bool) -> bool:
    # Strict mirrors the fleet's `os.environ["SENTINEL_ENABLED"] == "TRUE"`; lenient accepts any spelling.
    if strict:
        return _env("SENTINEL_ENABLED", strict=True, fallback="") == "TRUE"
    return env_bool("SENTINEL_ENABLED")


def redis_caches(
    *,
    sentinel_enabled: bool | None = None,
    strict: bool = True,
    alias: str = "default",
    db: str | int | None = None,
    password: str | None = None,
    host: str | None = None,
    port: str | int | None = None,
    sentinel_service: str | None = None,
    sentinel_hosts: Sequence[str] | str | None = None,
    sentinel_port: str | int | None = None,
    ignore_exceptions: bool = False,
    log_ignored_exceptions: bool = True,
    logger_name: str = DEFAULT_REDIS_LOGGER,
) -> RedisCacheSettings:
    """Both branches of the fleet's ``SENTINEL_ENABLED`` if/else, plus the django_redis knobs set alongside them."""
    if sentinel_enabled is None:
        sentinel_enabled = _sentinel_enabled(strict=strict)
    redis_db = _value(db, "REDIS_DB", strict=strict, fallback="0")
    redis_password = _value(password, "REDIS_PASSWORD", strict=strict, fallback="")
    # IGNORE_EXCEPTIONS defaults off, matching 11 of the 13 today; OPTIONS is read before the module-level name.
    options: dict[str, Any] = {"PASSWORD": redis_password, "IGNORE_EXCEPTIONS": ignore_exceptions}
    hosts: list[str] = []
    sentinels: list[tuple[str, str]] = []
    if sentinel_enabled:
        hosts = _resolve_sentinel_hosts(sentinel_hosts, strict=strict)
        if not hosts:
            raise ImproperlyConfigured(
                "SENTINEL_ENABLED is TRUE but SENTINEL_HOSTS/SENTINEL_HOST resolved to no hosts."
            )
        port_value = _value(sentinel_port, "SENTINEL_PORT", strict=strict, fallback="26379")
        sentinels = [(sentinel_host, port_value) for sentinel_host in hosts]
        service = _value(sentinel_service, "SENTINEL_SERVICE", strict=strict, fallback="mymaster")
        location = f"redis://{service}/{redis_db}"
        options |= {
            "CLIENT_CLASS": SENTINEL_CLIENT,
            "SENTINELS": sentinels,
            "CONNECTION_POOL_CLASS": SENTINEL_CONNECTION_POOL,
            "SENTINEL_KWARGS": {"password": redis_password},
        }
        connection_factory = SENTINEL_CONNECTION_FACTORY
    else:
        redis_host = _value(host, "REDIS_HOST", strict=strict, fallback="localhost")
        redis_port = _value(port, "REDIS_PORT", strict=strict, fallback="6379")
        location = f"redis://{redis_host}:{redis_port}/{redis_db}"
        options["CLIENT_CLASS"] = DEFAULT_CLIENT
        connection_factory = DEFAULT_CONNECTION_FACTORY
    return RedisCacheSettings(
        caches={alias: {"BACKEND": REDIS_CACHE_BACKEND, "LOCATION": location, "OPTIONS": options}},
        sentinels=sentinels,
        sentinel_hosts=hosts,
        connection_factory=connection_factory,
        ignore_exceptions=ignore_exceptions,
        log_ignored_exceptions=log_ignored_exceptions,
        logger_name=logger_name,
    )


def cache_ttls(
    *,
    ttl: int | None = None,
    default_ttl: int = 60 * 60,
    long_ttl: int | None = None,
    long_ttl_factor: int | None = None,
    enabled: bool | None = None,
) -> CacheTtls:
    """The TTL triplet with every member independently optional; bind only the names your settings already had."""
    resolved = env_int("CACHE_TTL", default_ttl) if ttl is None else ttl
    if long_ttl is None and long_ttl_factor is not None:
        long_ttl = resolved * long_ttl_factor
    return CacheTtls(
        ttl=resolved,
        long_ttl=long_ttl,
        enabled=env_bool("CACHE_ENABLED") if enabled is None else enabled,
    )
