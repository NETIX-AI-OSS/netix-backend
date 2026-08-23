"""The shared django-filter shapes: CSV/IN parsing and the schema annotations."""

from typing import Any

import django_filters
import pytest
from drf_spectacular.types import OpenApiTypes

from netix_backend.django.filters import (
    BASE_FILTERS,
    NAMED_BASE_FILTERS,
    CharArrayFilter,
    CharInFilter,
    IntArrayFilter,
    NumberArrayFilter,
    NumberInFilter,
    base_filters,
    named_base_filters,
)
from tests.models import ScopedWidget


class WidgetFilterSet(django_filters.FilterSet):
    labels = CharArrayFilter(field_name="label", lookup_expr="in")
    labels_in = CharInFilter(field_name="label", lookup_expr="in")
    ids = NumberInFilter(field_name="id", lookup_expr="in")
    orgs = IntArrayFilter(field_name="organization_id", lookup_expr="in")
    orgs_number = NumberArrayFilter(field_name="organization_id", lookup_expr="in")

    class Meta:
        model = ScopedWidget
        fields: list[str] = []


@pytest.fixture
def widgets(db: Any) -> list[ScopedWidget]:
    return [
        ScopedWidget.objects.create(label="alpha", organization_id=1),
        ScopedWidget.objects.create(label="beta", organization_id=2),
        ScopedWidget.objects.create(label="gamma", organization_id=3),
    ]


@pytest.mark.parametrize(
    ("parameter", "value", "expected"),
    [
        ("labels", "alpha,beta", {"alpha", "beta"}),
        ("labels_in", "gamma", {"gamma"}),
        ("orgs", "1,3", {"alpha", "gamma"}),
        ("orgs_number", "2", {"beta"}),
    ],
)
def test_csv_filters_split_on_commas(
    widgets: list[ScopedWidget], parameter: str, value: str, expected: set[str]
) -> None:
    filtered = WidgetFilterSet({parameter: value}, queryset=ScopedWidget.objects.all()).qs
    assert {row.label for row in filtered} == expected


def test_number_in_filter_matches_ids(widgets: list[ScopedWidget]) -> None:
    ids = f"{widgets[0].pk},{widgets[2].pk}"
    filtered = WidgetFilterSet({"ids": ids}, queryset=ScopedWidget.objects.all()).qs
    assert {row.pk for row in filtered} == {widgets[0].pk, widgets[2].pk}


def test_array_filters_carry_their_schema_annotation() -> None:
    assert IntArrayFilter._spectacular_annotation["field"] is OpenApiTypes.INT
    assert NumberArrayFilter._spectacular_annotation["field"] is OpenApiTypes.NUMBER
    assert not hasattr(CharArrayFilter, "_spectacular_annotation")


def test_field_list_helpers_return_fresh_lists() -> None:
    fields = base_filters("organization_id")
    fields.append("mutated")
    assert list(BASE_FILTERS) == ["id", "created_on"]
    assert base_filters() == ["id", "created_on"]
    assert named_base_filters("organization_id") == [*NAMED_BASE_FILTERS, "organization_id"]
