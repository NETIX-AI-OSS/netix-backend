"""Conservative Pydantic read acceleration for existing DRF serializers.

The DRF serializer remains the public contract and continues to own validation, writes,
schema generation, and fields that cannot be represented with identical semantics.  The
mixin only batches proven-safe scalar attribute reads through a Pydantic ``TypeAdapter``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, ClassVar, cast

from django.db import models
from django.utils.datastructures import MultiValueDict
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    create_model,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)
from rest_framework import fields as drf_fields
from rest_framework.exceptions import ErrorDetail, ValidationError
from rest_framework.fields import SkipField, is_simple_callable
from rest_framework.relations import PKOnlyObject
from rest_framework.serializers import ListSerializer, Serializer

__all__ = [
    "PydanticInputMixin",
    "PydanticListSerializer",
    "PydanticReadMixin",
    "build_output_adapter",
    "describe",
]


class _Unmappable(Exception):
    """A field whose DRF representation is not proven equivalent."""


def _string(value: Any) -> str | None:
    value = value() if is_simple_callable(value) else value
    return None if value is None else str(value)


def _boolean(field: drf_fields.BooleanField) -> Callable[[Any], bool | None]:
    def render(value: Any) -> bool | None:
        value = value() if is_simple_callable(value) else value
        comparable = value.lower() if isinstance(value, str) else value
        try:
            if comparable in field.TRUE_VALUES:
                return True
            if comparable in field.FALSE_VALUES:
                return False
            if comparable in field.NULL_VALUES and field.allow_null:
                return None
        except TypeError:
            pass
        return bool(value)

    return render


def _integer(value: Any) -> int | None:
    value = value() if is_simple_callable(value) else value
    return None if value is None else int(value)


def _floating(value: Any) -> float | None:
    value = value() if is_simple_callable(value) else value
    return None if value is None else float(value)


_STRING_FIELDS = {
    drf_fields.CharField,
    drf_fields.EmailField,
    drf_fields.IPAddressField,
    drf_fields.SlugField,
    drf_fields.URLField,
}


def _converter(field: drf_fields.Field) -> Callable[[Any], Any]:
    """Return a representation function only for an exact, built-in DRF field class."""
    field_type = type(field)
    if field_type in _STRING_FIELDS:
        return _string
    if field_type is drf_fields.BooleanField:
        return _boolean(cast(drf_fields.BooleanField, field))
    if field_type in {drf_fields.IntegerField, drf_fields.BigIntegerField}:
        return _integer
    if field_type is drf_fields.FloatField:
        return _floating
    # Date/time, decimal, UUID, JSON, relations, and ReadOnlyField all have settings or
    # input-shape behavior that a general converter cannot reproduce safely.
    raise _Unmappable(field_type.__name__)


def _source(name: str, field: drf_fields.Field) -> tuple[str, ...]:
    source = field.source or name
    if source == "*" or "." in source:
        raise _Unmappable("complex source")
    return (source,)


def _field_signature(name: str, field: drf_fields.Field) -> tuple[Any, ...]:
    """Include every setting that can change the generated field or its output."""
    return (
        name,
        type(field),
        field.source,
        field.required,
        field.allow_null,
        getattr(field, "format", None),
        getattr(field, "decimal_places", None),
        getattr(field, "coerce_to_string", None),
        getattr(field, "rounding", None),
        getattr(field, "normalize_output", None),
        type(getattr(field, "pk_field", None)),
    )


def describe(serializer: Serializer) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
    """Split the serializer's live read fields into Pydantic and DRF paths."""
    mapped: dict[str, tuple[str, ...]] = {}
    fallback: dict[str, str] = {}
    for name, field in serializer.fields.items():
        if field.write_only:
            continue
        try:
            if field.default is not drf_fields.empty:
                raise _Unmappable("field default")
            _converter(field)
            mapped[name] = _source(name, field)
        except _Unmappable as exc:
            fallback[name] = str(exc)
    return mapped, fallback


class _OutputAdapter:
    def __init__(self, serializer: Serializer) -> None:
        self.mapped, self.fallback = describe(serializer)
        self.nullable = {name for name in self.mapped if serializer.fields[name].allow_null}
        definitions: dict[str, Any] = {}
        for name, source in self.mapped.items():
            field = serializer.fields[name]
            converter = _converter(field)
            annotation = Annotated[Any, PlainSerializer(converter, return_type=Any)]
            default = None if not field.required else ...
            definitions[name] = (annotation, Field(default=default, validation_alias=source[0]))
        model = create_model(
            f"{type(serializer).__name__}PydanticOutput",
            __config__=ConfigDict(from_attributes=True, arbitrary_types_allowed=True),
            **definitions,
        )
        self.adapter: TypeAdapter[Any] = TypeAdapter(list[model])  # type: ignore[valid-type]

    def dump(self, instances: list[Any]) -> list[dict[str, Any]]:
        validated = self.adapter.validate_python(instances, from_attributes=True)
        rows = cast(list[dict[str, Any]], self.adapter.dump_python(validated, exclude_unset=True))
        for row in rows:
            for name in self.nullable:
                row.setdefault(name, None)
        return rows


_ADAPTERS: dict[tuple[type[Serializer], tuple[tuple[Any, ...], ...]], _OutputAdapter] = {}


def build_output_adapter(serializer: Serializer) -> _OutputAdapter:
    """Return an adapter cached by serializer class and the complete live field signature."""
    signature = tuple(
        _field_signature(name, field) for name, field in serializer.fields.items() if not field.write_only
    )
    key = (type(serializer), signature)
    adapter = _ADAPTERS.get(key)
    if adapter is None:
        adapter = _ADAPTERS[key] = _OutputAdapter(serializer)
    return adapter


class PydanticListSerializer(ListSerializer):  # pylint: disable=abstract-method
    """Batch the safe read path while preserving custom row representation."""

    def to_representation(self, data: Any) -> Any:
        child = cast(Any, self.child)
        if not getattr(type(child), "pydantic_read", False):
            return super().to_representation(data)
        if (
            type(child).to_representation is not PydanticReadMixin.to_representation
            or not child._can_batch_representation()
        ):
            return super().to_representation(data)
        items = list(data.all() if isinstance(data, models.Manager) else data)
        return child.dump_many(items)


class PydanticReadMixin:
    pydantic_read: ClassVar[bool] = True

    def _can_batch_representation(self) -> bool:
        mro = type(self).mro()
        mixin_index = mro.index(PydanticReadMixin)
        serializer_index = mro.index(Serializer)
        return not any("to_representation" in base.__dict__ for base in mro[mixin_index + 1 : serializer_index])

    @classmethod
    def many_init(cls, *args: Any, **kwargs: Any) -> ListSerializer:
        """Use batching unless the serializer declares a custom list serializer."""
        result = super().many_init(*args, **kwargs)  # type: ignore[misc]
        result_type = type(result)
        if result_type.to_representation is not ListSerializer.to_representation:
            return result
        if result_type is ListSerializer:
            result.__class__ = PydanticListSerializer
            return result
        # ADRF's stock ListSerializer adds ``adata`` without changing representation.
        accelerated = type(f"Pydantic{result_type.__name__}", (PydanticListSerializer, result_type), {})
        result.__class__ = accelerated
        return result

    def _drf_value(self, name: str, instance: Any) -> Any:
        field = cast(Any, self).fields[name]
        attribute = field.get_attribute(instance)
        check = attribute.pk if isinstance(attribute, PKOnlyObject) else attribute
        return None if check is None else field.to_representation(attribute)

    def dump_many(self, instances: list[Any]) -> list[dict[str, Any]]:
        serializer = cast(Serializer, self)
        adapter = build_output_adapter(serializer)
        live_fields = {name: field for name, field in serializer.fields.items() if not field.write_only}
        order = list(live_fields)
        rows = adapter.dump(instances)
        output: list[dict[str, Any]] = []
        for row, instance in zip(rows, instances, strict=True):
            for name in adapter.fallback:
                try:
                    row[name] = self._drf_value(name, instance)
                except SkipField:
                    row.pop(name, None)
            output.append({name: row[name] for name in order if name in row})
        return output

    def to_representation(self, instance: Any) -> dict[str, Any]:
        if not type(self).pydantic_read:
            return super().to_representation(instance)  # type: ignore[misc]
        if not self._can_batch_representation():
            return super().to_representation(instance)  # type: ignore[misc]
        return self.dump_many([instance])[0]


def _drf_errors(exc: PydanticValidationError) -> dict[str, Any]:
    """Translate Pydantic locations and codes into DRF's field-error envelope."""
    errors: dict[str, Any] = {}
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        location = list(error["loc"]) or ["non_field_errors"]
        detail = ErrorDetail(str(error["msg"]), code=str(error["type"]))
        cursor: Any = errors
        for index, part in enumerate(location):
            last = index == len(location) - 1
            next_is_index = not last and isinstance(location[index + 1], int)
            if isinstance(part, int):
                while len(cursor) <= part:
                    cursor.append(None)
                if last:
                    if cursor[part] is None:
                        cursor[part] = []
                    cursor[part].append(detail)
                else:
                    if cursor[part] is None:
                        cursor[part] = [] if next_is_index else {}
                    cursor = cursor[part]
            elif last:
                cursor.setdefault(str(part), []).append(detail)
            else:
                cursor = cursor.setdefault(str(part), [] if next_is_index else {})
    return errors


class PydanticInputMixin:
    # PATCH is explicit: mechanically optionalizing a create model can weaken cross-field validators.
    pydantic_model: ClassVar[type[BaseModel]]
    pydantic_partial_model: ClassVar[type[BaseModel] | None] = None

    def to_internal_value(self, data: Any) -> Any:
        model = self.pydantic_model
        partial = bool(getattr(cast(Any, self).root, "partial", cast(Any, self).partial))
        if partial:
            model = self.pydantic_partial_model  # type: ignore[assignment]
            if model is None:
                raise AssertionError(
                    f"{type(self).__name__} must declare pydantic_partial_model before it can validate PATCH data"
                )
        pydantic_data = data
        if isinstance(data, MultiValueDict):
            pydantic_data = dict(data.items())
            accepted = {name for name in model.model_fields}
            accepted.update(field.alias for field in model.model_fields.values() if isinstance(field.alias, str))
            for name, field in cast(Any, self).fields.items():
                if name in accepted and isinstance(field, (drf_fields.ListField, ListSerializer)):
                    value = field.get_value(data)
                    if value is not drf_fields.empty:
                        pydantic_data[name] = value
        try:
            model.model_validate(pydantic_data)
        except PydanticValidationError as exc:
            raise ValidationError(_drf_errors(exc)) from exc
        # Pydantic proves the explicit contract; DRF still owns accepted input types,
        # relation authorization, defaults, custom fields, and persistence values.
        return super().to_internal_value(data)  # type: ignore[misc]
