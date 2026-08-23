"""Shared serializer field constants and the model-less named base serializer."""

from typing import Any, Final

from rest_framework import serializers

__all__ = [
    "BASE_FIELDS",
    "NAMED_BASE_FIELDS",
    "NamedBaseSerializer",
    "base_fields",
    "named_base_fields",
]

BASE_FIELDS: Final[tuple[str, ...]] = ("id", "created_on", "updated_on")
NAMED_BASE_FIELDS: Final[tuple[str, ...]] = (*BASE_FIELDS, "name", "display_name", "description", "is_archived")


def base_fields(*extra: str) -> list[str]:
    """A fresh list of the base fields plus ``extra``; never share the constant, callers mutate it."""
    return [*BASE_FIELDS, *extra]


def named_base_fields(*extra: str) -> list[str]:
    """A fresh list of the named base fields plus ``extra``."""
    return [*NAMED_BASE_FIELDS, *extra]


class NamedBaseSerializer(serializers.ModelSerializer[Any]):
    """Model-less base carrying only ``Meta.fields``; subclasses bind their own ``Meta.model``."""

    class Meta:
        fields = named_base_fields()
