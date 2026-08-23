"""Tests for netix_backend.django.serializers."""

from netix_backend.django.serializers import (
    BASE_FIELDS,
    NAMED_BASE_FIELDS,
    NamedBaseSerializer,
    base_fields,
    named_base_fields,
)
from tests.models import NamedWidget


def test_constants_are_tuples_in_the_canonical_order():
    assert BASE_FIELDS == ("id", "created_on", "updated_on")
    assert NAMED_BASE_FIELDS == (
        "id",
        "created_on",
        "updated_on",
        "name",
        "display_name",
        "description",
        "is_archived",
    )


def test_helpers_return_fresh_lists():
    first = base_fields()
    first.append("mutated")
    assert base_fields() == list(BASE_FIELDS)
    assert base_fields("organization_id") == [*BASE_FIELDS, "organization_id"]

    named = named_base_fields()
    named.append("mutated")
    assert named_base_fields() == list(NAMED_BASE_FIELDS)
    assert named_base_fields("a", "b") == [*NAMED_BASE_FIELDS, "a", "b"]


def test_named_base_serializer_meta_is_model_less():
    assert not hasattr(NamedBaseSerializer.Meta, "model")
    assert NamedBaseSerializer.Meta.fields == list(NAMED_BASE_FIELDS)


def test_named_base_serializer_is_usable_as_a_base():
    class WidgetSerializer(NamedBaseSerializer):
        class Meta(NamedBaseSerializer.Meta):
            model = NamedWidget
            fields = named_base_fields()

    serializer = WidgetSerializer(NamedWidget(pk=1, name="n", display_name="N"))
    assert set(serializer.data) == set(NAMED_BASE_FIELDS)
    assert serializer.data["name"] == "n"
