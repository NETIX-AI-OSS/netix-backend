"""Postgres ``DATABASES`` aliases as one factory; pure stdlib and Django-free, so settings.py can import it first."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from netix_backend.env import OMIT, REQUIRED, _Omit, _Required

__all__ = (
    "OMIT",
    "POSTGIS_ENGINE",
    "POSTGRES_ENGINE",
    "REQUIRED",
    "FromEnv",
    "Value",
    "postgres_database",
    "replica_of",
)

POSTGRES_ENGINE: Final[str] = "django.db.backends.postgresql"
# asset-service is the only PostGIS service, but the string belongs beside the one it replaces.
POSTGIS_ENGINE: Final[str] = "django.contrib.gis.db.backends.postgis"

# Every knob takes one of three things: a literal Django sees verbatim, a FromEnv to read now, or OMIT to drop the key.
Value = Any


@dataclass(frozen=True, slots=True)
class FromEnv:
    """One environment read: a ``REQUIRED`` default reproduces ``os.environ[var]``, any other ``os.environ.get``."""

    var: str
    default: Any = REQUIRED
    cast: Callable[[Any], Any] | None = None

    def resolve(self) -> Any:
        """Read the variable now, exactly the way the settings line being replaced read it."""
        # Raw os.environ, never netix_backend.env._lookup: an empty POSTGRES_HOST must stay "", not become a default.
        raw = os.environ[self.var] if isinstance(self.default, _Required) else os.environ.get(self.var, self.default)
        return self.cast(raw) if self.cast is not None else raw


def _resolve(value: Value) -> Any:
    """A :class:`FromEnv` reads its variable now; anything else is already the literal Django will see."""
    return value.resolve() if isinstance(value, FromEnv) else value


def _put(entry: dict[str, Any], key: str, value: Value) -> None:
    """Set *key*, unless the caller passed ``OMIT`` to say the key must not appear in the alias at all."""
    if not isinstance(value, _Omit):
        entry[key] = _resolve(value)


def _override(entry: dict[str, Any], key: str, value: Value) -> None:
    """Like :func:`_put`, but for a copied alias where ``OMIT`` has to drop a key inherited from the primary."""
    if isinstance(value, _Omit):
        entry.pop(key, None)
    else:
        entry[key] = _resolve(value)


def _options(
    prepare_threshold: int | None | _Omit,
    connect_timeout: Value | _Omit,
    options: Mapping[str, Any] | _Omit,
) -> dict[str, Any] | None:
    """``OPTIONS`` only once a member is set: the nine services with no ``OPTIONS`` key today must keep having none."""
    if isinstance(prepare_threshold, _Omit) and isinstance(connect_timeout, _Omit) and isinstance(options, _Omit):
        return None
    resolved: dict[str, Any] = {}
    if not isinstance(prepare_threshold, _Omit):
        resolved["prepare_threshold"] = prepare_threshold
    if not isinstance(connect_timeout, _Omit):
        resolved["connect_timeout"] = _resolve(connect_timeout)
    if not isinstance(options, _Omit):
        resolved.update(options)
    return resolved


def _ordered(entry: dict[str, Any], key_order: Sequence[str] | None) -> dict[str, Any]:
    """Repin insertion order; Django reads ``DATABASES`` by key, so this only matters to a repr snapshot test."""
    if key_order is None:
        return entry
    ordered = {key: entry[key] for key in key_order if key in entry}
    ordered.update(entry)
    return ordered


def postgres_database(
    *,
    engine: Value = POSTGRES_ENGINE,
    name: Value = FromEnv("POSTGRES_NAME"),
    user: Value = FromEnv("POSTGRES_USER"),
    password: Value = FromEnv("POSTGRES_PASSWORD"),
    host: Value = FromEnv("POSTGRES_HOST"),
    port: Value = FromEnv("POSTGRES_PORT"),
    atomic_requests: bool | _Omit = False,
    disable_server_side_cursors: bool | _Omit = True,
    conn_max_age: Value = 0,
    conn_health_checks: bool | _Omit = OMIT,
    # OMIT drops "prepare_threshold" and, with no other member set, the whole OPTIONS key; None emits it as None.
    prepare_threshold: int | None | _Omit = OMIT,
    connect_timeout: Value | _Omit = OMIT,
    options: Mapping[str, Any] | _Omit = OMIT,
    test: Mapping[str, Any] | _Omit = OMIT,
    extra: Mapping[str, Any] | None = None,
    key_order: Sequence[str] | None = None,
) -> dict[str, Any]:
    """One ``DATABASES`` alias; every knob takes a literal, a :class:`FromEnv`, or ``OMIT`` to drop the key."""
    entry: dict[str, Any] = {}
    _put(entry, "ENGINE", engine)
    _put(entry, "NAME", name)
    _put(entry, "USER", user)
    _put(entry, "PASSWORD", password)
    _put(entry, "HOST", host)
    _put(entry, "PORT", port)
    _put(entry, "CONN_MAX_AGE", conn_max_age)
    _put(entry, "CONN_HEALTH_CHECKS", conn_health_checks)
    _put(entry, "ATOMIC_REQUESTS", atomic_requests)
    _put(entry, "DISABLE_SERVER_SIDE_CURSORS", disable_server_side_cursors)
    resolved_options = _options(prepare_threshold, connect_timeout, options)
    if resolved_options is not None:
        entry["OPTIONS"] = resolved_options
    _put(entry, "TEST", test)
    entry.update(extra or {})
    return _ordered(entry, key_order)


def replica_of(
    primary: Mapping[str, Any],
    *,
    host: Value,
    port: Value,
    test: Mapping[str, Any] | _Omit = OMIT,
    **overrides: Value,
) -> dict[str, Any]:
    """The ``{**DATABASES["default"], "HOST": ..., "PORT": ...}`` idiom, preserving the primary's key order."""
    replica = dict(primary)
    _override(replica, "HOST", host)
    _override(replica, "PORT", port)
    _override(replica, "TEST", test)
    # Lower-case argument names so the call site reads like postgres_database(); OMIT here drops an inherited key.
    for key, value in overrides.items():
        _override(replica, key.upper(), value)
    return replica
