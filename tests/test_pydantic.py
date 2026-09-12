"""Differential contracts for the conservative Pydantic serializer read path."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from adrf import serializers as async_serializers
from django.http import QueryDict
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rest_framework import serializers

from netix_backend.django import pydantic as bridge
from netix_backend.django.pydantic import PydanticInputMixin, PydanticListSerializer, PydanticReadMixin, describe


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


def test_nullable_missing_and_explicit_defaults_match_drf():
    class EdgeSerializer(serializers.Serializer):
        flag = serializers.BooleanField(allow_null=True)
        missing = serializers.CharField(required=False, allow_null=True)
        defaulted = serializers.CharField(default="fallback")

    class FastEdgeSerializer(PydanticReadMixin, EdgeSerializer):
        pass

    for value in ({"flag": "FaLsE"}, {"flag": None}):
        assert FastEdgeSerializer(value).data == EdgeSerializer(value).data


def test_zero_argument_source_method_uses_drf_callable_semantics():
    class CallableRecord:
        def get_name(self) -> str:
            return "called"

    class CallableSerializer(PydanticReadMixin, serializers.Serializer):
        name = serializers.CharField(source="get_name")
        count = serializers.IntegerField(source="get_count")

    CallableRecord.get_count = lambda self: "4"  # type: ignore[attr-defined]
    assert CallableSerializer(CallableRecord()).data == {"name": "called", "count": 4}


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


def test_intermediate_base_representation_is_not_bypassed_by_batching():
    class HookBase(serializers.Serializer):
        name = serializers.CharField()

        def to_representation(self, instance: Any) -> Any:
            data = super().to_representation(instance)
            data["base_hook"] = True
            return data

    class FastHookSerializer(PydanticReadMixin, HookBase):
        pass

    assert FastHookSerializer([Record()], many=True).data == [{"name": "7", "base_hook": True}]


@pytest.mark.asyncio
async def test_adrf_adata_surface_is_preserved():
    class AsyncSerializer(PydanticReadMixin, async_serializers.Serializer):
        name = serializers.CharField()

    serializer = AsyncSerializer([Record()], many=True)
    assert await serializer.adata == [{"name": "7"}]


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


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=3)
    count: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def name_and_count_agree(self) -> CreateRequest:
        if self.name == "many" and self.count < 2:
            raise ValueError("many requires at least two")
        return self


class PatchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=3)
    count: int | None = Field(default=None, ge=1)


class NestedItem(BaseModel):
    count: int = Field(ge=1)


class NestedRequest(BaseModel):
    items: list[NestedItem]


class ExplicitInputSerializer(PydanticInputMixin, serializers.Serializer):
    pydantic_model = CreateRequest
    pydantic_partial_model = PatchRequest

    name = serializers.CharField()
    count = serializers.IntegerField(default=9)
    relation = serializers.IntegerField(required=False, min_value=10)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        if attrs.get("name") == "drf":
            raise serializers.ValidationError({"name": "DRF hook ran"})
        return attrs


def test_explicit_input_model_normalizes_defaults_then_keeps_drf_fields_and_hooks():
    serializer = ExplicitInputSerializer(data={"name": "item", "relation": 12})
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"name": "item", "count": 9, "relation": 12}

    drf_hook = ExplicitInputSerializer(data={"name": "drf"})
    assert not drf_hook.is_valid()
    assert "name" in drf_hook.errors


def test_explicit_input_errors_have_drf_field_keys_and_pydantic_codes():
    serializer = ExplicitInputSerializer(data={"name": "x"})
    assert not serializer.is_valid()
    assert serializer.errors["name"][0].code == "string_too_short"

    cross_field = ExplicitInputSerializer(data={"name": "many", "count": 1})
    assert not cross_field.is_valid()
    assert cross_field.errors["non_field_errors"][0].code == "value_error"

    class NestedInput(PydanticInputMixin, serializers.Serializer):
        pydantic_model = NestedRequest
        items = serializers.ListField(child=serializers.DictField())

    nested = NestedInput(data={"items": [{"count": 0}]})
    assert not nested.is_valid()
    assert nested.errors["items"][0]["count"][0].code == "greater_than_equal"


def test_partial_input_uses_explicit_patch_contract_without_applying_defaults():
    serializer = ExplicitInputSerializer(data={"count": "3"}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"count": 3}

    class UnsafePartial(PydanticInputMixin, serializers.Serializer):
        pydantic_model = CreateRequest
        name = serializers.CharField()

    with pytest.raises(AssertionError, match="pydantic_partial_model"):
        UnsafePartial(data={}, partial=True).is_valid()


def test_input_preserves_querydict_values_for_drf_only_fields():
    data = QueryDict("name=item&relation=12")
    serializer = ExplicitInputSerializer(data=data)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["relation"] == 12


def test_input_preserves_repeated_querydict_values_for_explicit_list_fields():
    class ListRequest(BaseModel):
        tags: list[str]

    class ListInput(PydanticInputMixin, serializers.Serializer):
        pydantic_model = ListRequest
        tags = serializers.ListField(child=serializers.CharField())

    data = QueryDict("tags=a&tags=b")
    serializer = ListInput(data=data)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data == {"tags": ["a", "b"]}

    class ListPatch(BaseModel):
        tags: list[str] | None = None

    ListInput.pydantic_partial_model = ListPatch
    missing = ListInput(data=QueryDict(""), partial=True)
    assert missing.is_valid(), missing.errors
    assert missing.validated_data == {}


def test_conservative_converter_edge_paths_and_opt_out():
    class EdgeSerializer(PydanticReadMixin, serializers.Serializer):
        pydantic_read = False
        hidden = serializers.CharField(write_only=True)
        dotted = serializers.CharField(source="nested.name")

    assert EdgeSerializer(SimpleNamespace(nested=SimpleNamespace(name="n"))).data == {"dotted": "n"}
    mapped, fallback = describe(EdgeSerializer())
    assert not mapped
    assert fallback == {"dotted": "complex source"}

    boolean = bridge._boolean(serializers.BooleanField(allow_null=True))
    assert boolean(None) is None
    assert boolean("TRUE") is True
    assert boolean([]) is False
    assert boolean("unknown") is True
    assert bridge._string(None) is None
    assert bridge._integer(None) is None
    assert bridge._floating(None) is None


def test_list_opt_out_and_fallback_skipfield_paths():
    class Plain(serializers.Serializer):
        name = serializers.CharField()

    plain = Plain()
    assert PydanticListSerializer(child=plain).to_representation([Record()]) == [{"name": "7"}]

    class OptionalCustom(serializers.Field):
        def to_representation(self, value: Any) -> Any:
            return value

        def to_internal_value(self, data: Any) -> Any:
            return data

    class FastOptional(PydanticReadMixin, serializers.Serializer):
        missing = OptionalCustom(required=False)

    assert FastOptional(SimpleNamespace()).data == {}


def test_nested_error_builder_handles_direct_list_locations_and_repeated_errors():
    class FakeError:
        def errors(self, **kwargs: Any) -> list[dict[str, Any]]:
            return [
                {"loc": ("items", 0), "msg": "first", "type": "bad"},
                {"loc": ("items", 0), "msg": "second", "type": "worse"},
                {"loc": ("matrix", 0, 0), "msg": "cell", "type": "bad"},
                {"loc": ("matrix", 0, 1), "msg": "cell two", "type": "bad"},
            ]

    errors = bridge._drf_errors(FakeError())  # type: ignore[arg-type]
    assert [detail.code for detail in errors["items"][0]] == ["bad", "worse"]
    assert errors["matrix"][0][0][0].code == "bad"
