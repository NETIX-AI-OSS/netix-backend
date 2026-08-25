"""The two-phase test-settings recipe 18 app/settings_test.py modules hand-roll: apply env, then splat overrides."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Any, Final, Literal

from netix_backend.django.testing import (
    DEFAULT_TEST_BEARER,
    EXPLICIT_MIDDLEWARE_PATH,
    TEST_ENV_DEFAULTS,
    TEST_MIDDLEWARE_PATH,
    platform_test_identity,
    swap_auth_middleware,
)

# Settings-time module: it runs before django.setup(), so django.conf must not be imported at module scope.

# The module and its test_overrides() match pytest's default collection patterns; this is what stops the collector.
__test__ = False

__all__ = (
    "CELERY_EAGER_SETTINGS",
    "LOCMEM_CACHE_BACKEND",
    "LOCMEM_EMAIL_BACKEND",
    "MD5_PASSWORD_HASHER",
    "MODE_MIDDLEWARE",
    "SESSION_ENGINES",
    "SQLITE_ENGINE",
    "TEST_ENV_DEFAULTS",
    "DisableMigrations",
    "EnvoySpec",
    "apply_test_env",
    "load_base_settings",
    "quiet_db_logging_config",
    "test_overrides",
)

SQLITE_ENGINE: Final[str] = "django.db.backends.sqlite3"
LOCMEM_CACHE_BACKEND: Final[str] = "django.core.cache.backends.locmem.LocMemCache"
LOCMEM_EMAIL_BACKEND: Final[str] = "django.core.mail.backends.locmem.EmailBackend"
MD5_PASSWORD_HASHER: Final[str] = "django.contrib.auth.hashers.MD5PasswordHasher"

SESSION_ENGINES: Final[dict[str, str]] = {
    "cache": "django.contrib.sessions.backends.cache",
    "signed_cookies": "django.contrib.sessions.backends.signed_cookies",
}

# mode="none" keeps the production middleware in place; report-engine mirrors prod and injects identity per fixture.
MODE_MIDDLEWARE: Final[dict[str, str | None]] = {
    "resolver": TEST_MIDDLEWARE_PATH,
    "explicit": EXPLICIT_MIDDLEWARE_PATH,
    "none": None,
}

CELERY_EAGER_SETTINGS: Final[dict[str, Any]] = {
    "CELERY_TASK_ALWAYS_EAGER": True,
    # Eager tasks must not re-raise: a task failure is asserted on the result, not on the caller's stack.
    "CELERY_TASK_EAGER_PROPAGATES": False,
    "CELERY_BROKER_URL": "memory://",
    "CELERY_RESULT_BACKEND": "cache+memory://",
}


class DisableMigrations:
    """The ``MIGRATION_MODULES`` stand-in that makes Django create every table straight from the models."""

    def __contains__(self, item: str) -> bool:
        return True

    def __getitem__(self, item: str) -> None:
        return None


@dataclass(frozen=True)
class EnvoySpec:
    """How a suite obtains ``request.envoy``: the identity, the bearer, and which middleware resolves them."""

    permissions: Sequence[str] = ()
    username: str | None = None
    bearer: str | None = None
    mode: Literal["resolver", "explicit", "none"] = "resolver"
    middleware: str | None = None
    identity: Mapping[str, Any] | None = None


def apply_test_env(
    *,
    force: Mapping[str, str] | None = None,
    exclude: Sequence[str] = (),
    **overrides: str,
) -> dict[str, str]:
    """Setdefault the NETIX test-env baseline plus the caller's overrides, then assign ``force`` unconditionally.

    ``force`` exists because an inherited value can be actively harmful: cafm-backend pins its Redis and
    Sentinel hosts so an exported ``REDIS_HOST`` cannot hang a Celery ``.delay()`` on an unreachable box.
    ``exclude`` is the opposite escape hatch: user-management derives its MFA keys from ``DJANGO_SECRET``
    and must be able to leave that key unset. Returns the resulting environment for every key it managed.
    """
    skipped = set(exclude)
    defaults = {**TEST_ENV_DEFAULTS, **overrides}
    forced = dict(force or {})
    for key, value in defaults.items():
        if key not in skipped:
            os.environ.setdefault(key, value)
    for key, value in forced.items():
        if key not in skipped:
            os.environ[key] = value
    return {key: os.environ[key] for key in {**defaults, **forced} if key not in skipped}


def load_base_settings(
    namespace: dict[str, Any],
    module: str = "app.settings",
    *,
    skip_existing: bool = False,
) -> ModuleType:
    """Import the base settings module and copy its uppercase names into ``namespace``, returning the module.

    ``skip_existing=True`` reproduces asset-service's ``not in globals()`` guard, for a test settings module
    that binds a name before the base import. Repos that already write ``from .settings import *`` keep doing
    that and hand the module they imported straight to :func:`test_overrides`.
    """
    base = import_module(module)
    for name in dir(base):
        if name.isupper() and not (skip_existing and name in namespace):
            namespace[name] = getattr(base, name)
    return base


def quiet_db_logging_config() -> dict[str, Any]:
    """A fresh ``LOGGING`` dict that swallows ``django.db.backends`` query logs under ``DEBUG=True``."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {"console": {"class": "logging.NullHandler"}},
        "loggers": {"django.db.backends": {"handlers": ["console"], "level": "ERROR", "propagate": False}},
    }


def _database_config(
    database: str | os.PathLike[str] | Mapping[str, Any],
    engine: str,
    atomic_requests: bool | None,
) -> dict[str, Any]:
    """A mapping is taken verbatim as the alias configuration; anything else is a SQLite ``NAME``."""
    config: dict[str, Any] = dict(database) if isinstance(database, Mapping) else {"ENGINE": engine, "NAME": database}
    if atomic_requests is not None:
        config["ATOMIC_REQUESTS"] = atomic_requests
    return config


def _databases(
    database: str | os.PathLike[str] | Mapping[str, Any],
    engine: str,
    aliases: Sequence[str],
    *,
    alias_mirror: bool,
    atomic_requests: bool | None,
) -> dict[str, Any]:
    """Every extra alias is a clone of ``default``, which is how the read-replica aliases are declared today."""
    default = _database_config(database, engine, atomic_requests)
    databases: dict[str, Any] = {"default": default}
    for alias in aliases:
        clone = dict(default)
        if alias_mirror:
            clone["TEST"] = {"MIRROR": "default"}
        databases[alias] = clone
    return databases


def _caches(cache_location: str | None) -> dict[str, Any]:
    """Locmem, with the per-repo LOCATION that keeps parallel xdist workers out of each other's cache."""
    config: dict[str, Any] = {"BACKEND": LOCMEM_CACHE_BACKEND}
    if cache_location is not None:
        config["LOCATION"] = cache_location
    return {"default": config}


def _static_root(static_root: str | bool) -> str:
    """``True`` takes a throwaway temp directory; a path is created so collectstatic never has to."""
    if static_root is True:
        return tempfile.mkdtemp()
    path = str(static_root)
    os.makedirs(path, exist_ok=True)
    return path


def _migration_modules(disable_migrations: bool | Sequence[str]) -> Any:
    """``True`` disables every app's migrations; a sequence disables only the labels it names."""
    if isinstance(disable_migrations, bool):
        return DisableMigrations()
    return {label: None for label in disable_migrations}


def _envoy_overrides(base: ModuleType | None, spec: EnvoySpec) -> dict[str, Any]:
    """The identity, bearer and MIDDLEWARE swap the seven resolver-seam repos write out by hand."""
    if spec.identity is None:
        named = {} if spec.username is None else {"username": spec.username}
        identity: dict[str, Any] = platform_test_identity(permissions=spec.permissions, **named)
    else:
        identity = dict(spec.identity)
    bearer = spec.bearer
    if bearer is None:
        bearer = DEFAULT_TEST_BEARER if spec.username is None else f"Bearer {spec.username}"
    overrides: dict[str, Any] = {"NETIX_TEST_ENVOY_IDENTITY": identity, "NETIX_TEST_ENVOY_BEARER": bearer}
    replacement = spec.middleware or MODE_MIDDLEWARE[spec.mode]
    # A standalone test settings module has no base MIDDLEWARE to swap, so it takes the identity alone.
    if base is not None and replacement is not None:
        overrides["MIDDLEWARE"] = swap_auth_middleware(base.MIDDLEWARE, replacement)
    return overrides


def test_overrides(
    base: ModuleType | None = None,
    *,
    database: str | os.PathLike[str] | Mapping[str, Any] = ":memory:",
    engine: str = SQLITE_ENGINE,
    aliases: Sequence[str] = (),
    alias_mirror: bool = False,
    clear_routers: bool = True,
    atomic_requests: bool | None = None,
    cache_location: str | None = None,
    session_backend: Literal["cache", "signed_cookies"] | None = None,
    fast_hashers: bool = False,
    disable_migrations: bool | Sequence[str] = False,
    static_root: str | bool = False,
    email_locmem: bool = False,
    celery_eager: bool = False,
    quiet_db_logging: bool = False,
    envoy: EnvoySpec | None = None,
) -> dict[str, Any]:
    """The uppercase overrides a test settings module splats into its namespace after the base import."""
    overrides: dict[str, Any] = {
        "DATABASES": _databases(database, engine, aliases, alias_mirror=alias_mirror, atomic_requests=atomic_requests),
        "CACHES": _caches(cache_location),
    }
    flags: tuple[tuple[bool, dict[str, Any]], ...] = (
        # Django's own default is [], so clearing is a no-op unless the service declares a read-replica router.
        (clear_routers, {"DATABASE_ROUTERS": []}),
        (fast_hashers, {"PASSWORD_HASHERS": [MD5_PASSWORD_HASHER]}),
        (email_locmem, {"EMAIL_BACKEND": LOCMEM_EMAIL_BACKEND}),
        (celery_eager, dict(CELERY_EAGER_SETTINGS)),
        (quiet_db_logging, {"LOGGING": quiet_db_logging_config()}),
    )
    for enabled, fragment in flags:
        if enabled:
            overrides.update(fragment)
    if session_backend is not None:
        overrides["SESSION_ENGINE"] = SESSION_ENGINES[session_backend]
    if disable_migrations:
        overrides["MIGRATION_MODULES"] = _migration_modules(disable_migrations)
    if static_root:
        overrides["STATIC_ROOT"] = _static_root(static_root)
    if envoy is not None:
        overrides.update(_envoy_overrides(base, envoy))
    return overrides


# The name matches pytest's default python_functions pattern, so it is marked wherever a suite imports it.
test_overrides.__test__ = False  # type: ignore[attr-defined]
