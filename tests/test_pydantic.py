"""Differential contracts for the conservative Pydantic serializer read path."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from django.utils import timezone
from rest_framework import serializers

from netix_backend.django.pydantic import PydanticListSerializer, PydanticReadMixin, describe


@dataclass
class Record:
    name: Any = 7
    enabled: Any = "false"
    count: Any = "4"
    ratio: Any = "1.5"
    price: Any = Decimal("2.1")
    created: dt.datetime = dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.UTC)
    optional: Any = None
    identifier: uuid.UUID = uuid.UUID("6f41108c-bc75-4f19-8bd2-86bdc843e9a5")

    @property
    def secret(self) -> str:
        raise AssertionError("a dynamically omitted field was fetched")


class NestedSerializer(serializers.Serializer):
    label = serializers.CharField()


class ContractSerializer(serializers.Serializer):
    name = serializers.CharField()
    enabled = serializers.BooleanField()
    count = serializers.IntegerField()
    ratio = serializers.FloatField()
    price = serializers.DecimalField(max_digits=5, decimal_places=2)
    created = serializers.DateTimeField()
    alias = serializers.CharField(source="name")
    nested = NestedSerializer()
    calculated = serializers.SerializerMethodField()
    optional = serializers.CharField(required=False, allow_null=True)
    identifier = serializers.UUIDField()

    def get_calculated(self, instance: Any) -> str:
        return f"{instance.name}!"


class FastContractSerializer(PydanticReadMixin, ContractSerializer):
    pass


def test_scalar_fast_path_is_differential_with_nested_method_alias_and_datetime_fallback():
    record = Record()
    record.nested = SimpleNamespace(label="nested")  # type: ignore[attr-defined]

    expected = ContractSerializer([record, record], many=True).data
    actual = FastContractSerializer([record, record], many=True).data

    assert actual == expected
    assert isinstance(FastContractSerializer(many=True), PydanticListSerializer)
    mapped, fallback = describe(FastContractSerializer())
    assert {"name", "enabled", "count", "ratio", "alias", "optional"} <= mapped.keys()
    assert {"price", "created", "nested", "calculated", "identifier"} <= fallback.keys()
    assert actual[0]["enabled"] is False


def test_current_timezone_datetime_stays_on_drf_path(settings):
    settings.TIME_ZONE = "Asia/Kolkata"
    timezone.activate(settings.TIME_ZONE)
    try:
        record = Record()
        record.nested = SimpleNamespace(label="nested")  # type: ignore[attr-defined]
        expected = ContractSerializer(record).data["created"]
        assert FastContractSerializer(record).data["created"] == expected == "2026-01-02T08:34:00+05:30"
    finally:
        timezone.deactivate()


class DynamicSerializer(PydanticReadMixin, serializers.Serializer):
    value = serializers.CharField(source="name")
    secret = serializers.CharField()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        value_type = kwargs.pop("value_type", "string")
        include_secret = kwargs.pop("include_secret", False)
        super().__init__(*args, **kwargs)
        if value_type == "integer":
            self.fields["value"] = serializers.IntegerField(source="name")
        if not include_secret:
            self.fields.pop("secret")


def test_live_dynamic_fields_are_neither_reused_by_name_nor_fetched_when_omitted():
    record = Record(name="8")
    assert DynamicSerializer(record).data == {"value": "8"}
    assert DynamicSerializer(record, value_type="integer").data == {"value": 8}


class CustomCharField(serializers.CharField):
    def to_representation(self, value: Any) -> str:
        return f"custom:{value}"


class CustomFieldSerializer(PydanticReadMixin, serializers.Serializer):
    name = CustomCharField()


def test_custom_field_subclasses_keep_their_drf_representation():
    assert CustomFieldSerializer(Record()).data == {"name": "custom:7"}


class CustomListSerializer(serializers.ListSerializer):
    def to_representation(self, data: Any) -> Any:
        return list(reversed(super().to_representation(data)))


class CustomListChild(PydanticReadMixin, serializers.Serializer):
    name = serializers.CharField()

    class Meta:
        list_serializer_class = CustomListSerializer


def test_custom_list_serializer_hook_is_preserved():
    serializer = CustomListChild([Record(name="first"), Record(name="second")], many=True)
    assert type(serializer) is CustomListSerializer
    assert serializer.data == [{"name": "second"}, {"name": "first"}]


class PostProcessingSerializer(PydanticReadMixin, serializers.Serializer):
    name = serializers.CharField()

    def to_representation(self, instance: Any) -> Any:
        data = super().to_representation(instance)
        data["postprocessed"] = True
        return data


def test_subclass_row_representation_is_not_bypassed_by_batching():
    assert PostProcessingSerializer([Record()], many=True).data == [{"name": "7", "postprocessed": True}]


def test_write_validation_partial_and_save_hooks_remain_drf_owned():
    class WritableSerializer(PydanticReadMixin, serializers.Serializer):
        name = serializers.CharField()

        def create(self, validated_data: dict[str, Any]) -> Any:
            return SimpleNamespace(**validated_data)

        def update(self, instance: Any, validated_data: dict[str, Any]) -> Any:
            for key, value in validated_data.items():
                setattr(instance, key, value)
            return instance

    invalid = WritableSerializer(data={})
    assert not invalid.is_valid()
    assert invalid.errors["name"][0].code == "required"

    partial = WritableSerializer(SimpleNamespace(name="old"), data={}, partial=True)
    assert partial.is_valid(), partial.errors
    assert partial.save().name == "old"

    create = WritableSerializer(data={"name": "new"})
    assert create.is_valid(), create.errors
    assert create.save().name == "new"
