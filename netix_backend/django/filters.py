"""Shared django-filter CSV/IN filter shapes and the base filterset field lists."""

from __future__ import annotations

from typing import Final

from django_filters import BaseCSVFilter, BaseInFilter, CharFilter, NumberFilter  # type: ignore[import-untyped]
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field

__all__ = [
    "BASE_FILTERS",
    "NAMED_BASE_FILTERS",
    "CharArrayFilter",
    "CharInFilter",
    "IntArrayFilter",
    "NumberArrayFilter",
    "NumberInFilter",
    "base_filters",
    "named_base_filters",
]

BASE_FILTERS: Final[tuple[str, ...]] = ("id", "created_on")
NAMED_BASE_FILTERS: Final[tuple[str, ...]] = (*BASE_FILTERS, "name", "display_name", "is_archived")


def base_filters(*extra: str) -> list[str]:
    """A fresh list of the base filterset fields plus ``extra``."""
    return [*BASE_FILTERS, *extra]


def named_base_filters(*extra: str) -> list[str]:
    """A fresh list of the named filterset fields plus ``extra``."""
    return [*NAMED_BASE_FILTERS, *extra]


class CharArrayFilter(BaseCSVFilter, CharFilter):
    """``?field=a,b`` against a char column or an ArrayField of char."""


class CharInFilter(BaseInFilter, CharFilter):
    """``?field__in=a,b`` against a char column."""


class NumberInFilter(BaseInFilter, NumberFilter):
    """``?field__in=1,2`` against a numeric column."""


@extend_schema_field(OpenApiTypes.INT)
class IntArrayFilter(BaseCSVFilter, NumberFilter):
    """``?field=1,2`` against an ArrayField of IntegerField; annotated so the schema says integer."""


@extend_schema_field(OpenApiTypes.NUMBER)
class NumberArrayFilter(BaseCSVFilter, NumberFilter):
    """``?field=1.5,2`` against an ArrayField of a non-integer numeric field."""
