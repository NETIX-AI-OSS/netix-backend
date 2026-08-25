"""Org-prefixed machine keys and provenance columns for cloned primitives; importable without Django."""

from __future__ import annotations

import re
from typing import Final

__all__ = (
    "ORG_KEY_PREFIX",
    "PROVENANCE_FIELDS",
    "TEMPLATE_ORG_ID",
    "base_key",
    "is_org_key",
    "key_owner",
    "org_key",
    "org_prefix",
)

# Organization id of the platform template catalog every clone is taken from.
TEMPLATE_ORG_ID: Final[int] = 0

ORG_KEY_PREFIX: Final[str] = "nc"
# re.DOTALL so a key carrying a newline still matches; keys are machine identifiers, not display text.
_ORG_KEY_RE = re.compile(rf"^{ORG_KEY_PREFIX}(?P<org>\d+)_(?P<base>.*)$", re.DOTALL)

# Plain columns, never foreign keys: a cascade from a template onto live tenant rows is what must not happen.
PROVENANCE_FIELDS: Final[tuple[str, ...]] = ("cloned_from_id", "template_revision", "is_customized")


def org_prefix(org_id: int) -> str:
    """The key prefix owned by ``org_id`` — ``nc3_`` for organization 3."""
    return f"{ORG_KEY_PREFIX}{int(org_id)}_"


def base_key(name: str) -> str:
    """The template-relative key: ``nc3_fire_alarm`` -> ``fire_alarm``, others unchanged."""
    match = _ORG_KEY_RE.match(name or "")
    return match.group("base") if match else (name or "")


def org_key(name: str, org_id: int) -> str:
    """Return ``name`` as ``org_id``'s machine key; idempotent, and re-homing replaces rather than nests."""
    return f"{org_prefix(org_id)}{base_key(name)}"


def key_owner(name: str) -> int | None:
    """The organization a prefixed key belongs to, or ``None`` when it carries no prefix."""
    match = _ORG_KEY_RE.match(name or "")
    return int(match.group("org")) if match else None


def is_org_key(name: str, org_id: int) -> bool:
    """Whether ``name`` is already ``org_id``'s key."""
    return key_owner(name) == int(org_id)
