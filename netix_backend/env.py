"""Environment parsing shared by every NETIX service; pure stdlib, importable without Django."""

from __future__ import annotations

import logging
import os
from typing import Final, Literal, overload

TRUTHY = frozenset({"1", "true", "yes", "on"})
FALSY = frozenset({"0", "false", "no", "off"})

logger = logging.getLogger(__name__)


class ConfigurationError(RuntimeError):
    """A required environment variable is missing or cannot be parsed."""


class _Omit:
    """Sentinel meaning "leave this key out entirely", which is not the same as setting it to None or False."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "OMIT"


# Homed here rather than in either settings factory, so the DATABASES and Sentry helpers share one sentinel identity.
OMIT: Final = _Omit()


class _Required:
    """Sentinel marking a value that must be read strictly, the way ``os.environ[name]`` reads it."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "REQUIRED"


REQUIRED: Final = _Required()


def _lookup(name: str) -> str | None:
    """Return the stripped value of *name*, treating an empty or whitespace-only variable as unset."""
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def parse_bool(value: str | None, default: bool = False) -> bool:
    """Parse a ConfigMap boolean; ``None`` and unrecognised spellings fall back to *default*."""
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in TRUTHY:
        return True
    if normalized in FALSY:
        return False
    return default


def env_bool(name: str, default: bool | str = False) -> bool:
    """Case/spelling-insensitive boolean read, since ConfigMaps spell booleans inconsistently ('1', 'TRUE', 'true')."""
    # str defaults are the asset-service compatibility shim; new call sites pass a real bool.
    fallback = default if isinstance(default, bool) else parse_bool(default)
    return parse_bool(_lookup(name), fallback)


def env_bool_strict(name: str, default: bool = False, *, log: logging.Logger | None = None) -> bool:
    """Like :func:`env_bool` but logs an ERROR naming the variable and both accepted sets before falling back."""
    raw = _lookup(name)
    if raw is None:
        return default
    normalized = raw.lower()
    if normalized in TRUTHY:
        return True
    if normalized in FALSY:
        return False
    (log or logger).error(
        "%s=%r is not a recognised boolean (true: %s / false: %s); treating it as %s — fix the ConfigMap.",
        name,
        raw,
        sorted(TRUTHY),
        sorted(FALSY),
        default,
    )
    return default


@overload
def env_str(name: str, default: str, *, required: bool = ...) -> str: ...


@overload
def env_str(name: str, default: None = ..., *, required: Literal[True]) -> str: ...


@overload
def env_str(name: str, default: None = ..., *, required: bool = ...) -> str | None: ...


def env_str(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    """Read a string variable; ``required=True`` raises :class:`ConfigurationError` instead of a bare ``KeyError``."""
    value = _lookup(name)
    if value is not None:
        return value
    if required:
        raise ConfigurationError(f"Missing required environment variable {name}")
    return default


@overload
def env_first(*names: str, default: str, required: bool = ...) -> str: ...


@overload
def env_first(*names: str, default: None = ..., required: Literal[True]) -> str: ...


@overload
def env_first(*names: str, default: None = ..., required: bool = ...) -> str | None: ...


def env_first(*names: str, default: str | None = None, required: bool = False) -> str | None:
    """First non-empty value among *names*, so a service can borrow another service's key."""
    for name in names:
        value = _lookup(name)
        if value is not None:
            return value
    if required:
        raise ConfigurationError(f"Missing required environment variable {' or '.join(names)}")
    return default


def env_int(name: str, default: int) -> int:
    """Read an integer variable; an unset or empty value yields *default*."""
    raw = _lookup(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name}={raw!r} is not an integer") from exc


def env_float(name: str, default: float) -> float:
    """Read a float variable; an unset or empty value yields *default*."""
    raw = _lookup(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name}={raw!r} is not a number") from exc


def service_authorization(*env_names: str, fallback: str = "USER_SVC_AUTH") -> str:
    """Return the pod's own credential; never "" because envoy-pyauth reads a header-less call as org 0 superuser."""
    names = (*env_names, fallback) if fallback else env_names
    for name in names:
        value = _lookup(name)
        if value is not None:
            return value
    raise ConfigurationError(f"Missing {' or '.join(names)} environment variable")


__all__ = (
    "FALSY",
    "OMIT",
    "REQUIRED",
    "TRUTHY",
    "ConfigurationError",
    "env_bool",
    "env_bool_strict",
    "env_first",
    "env_float",
    "env_int",
    "env_str",
    "parse_bool",
    "service_authorization",
)
