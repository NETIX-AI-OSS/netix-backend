"""Shared serializer field constants, the model-less named base serializer, and the change-trail mixin."""

from typing import Any, Final

from rest_framework import serializers

from netix_backend.django.change_history import (
    MAX_REASON_LENGTH,
    REASON_FIELD,
    history_requested,
    reason_context,
)

__all__ = [
    "BASE_FIELDS",
    "NAMED_BASE_FIELDS",
    "ChangeHistoryChangeSerializer",
    "ChangeHistoryEntrySerializer",
    "ChangeHistorySerializerMixin",
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


class ChangeHistoryChangeSerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """One field that moved within a change entry."""

    field = serializers.CharField(read_only=True)
    old = serializers.JSONField(read_only=True, allow_null=True)
    new = serializers.JSONField(read_only=True, allow_null=True)
    # Only for choice fields, so the client needn't know status 1 means PRESENT.
    old_display = serializers.CharField(read_only=True, required=False)
    new_display = serializers.CharField(read_only=True, required=False)


class ChangeHistoryEntrySerializer(serializers.Serializer):  # pylint: disable=abstract-method
    """A single "who changed what, when" record."""

    at = serializers.DateTimeField(read_only=True)
    action = serializers.CharField(read_only=True, help_text="created | updated | deleted | restored")
    # Type-checker-only shadow of Field.source: DRF's metaclass moves declared fields off the class,
    # so the name never collides with the attribute the base Field uses.
    source = serializers.CharField(  # type: ignore[assignment]
        read_only=True, help_text="api | upload | system | mobile"
    )
    by = serializers.IntegerField(read_only=True, allow_null=True, help_text="Acting user id, null for system")
    by_name = serializers.CharField(read_only=True, allow_blank=True)
    # Why the change was made — empty when the write stated none, and null on entries recorded
    # before the field existed.
    reason = serializers.CharField(read_only=True, allow_blank=True, allow_null=True)
    changes = ChangeHistoryChangeSerializer(many=True, read_only=True)


class ChangeHistorySerializerMixin(serializers.Serializer):  # pylint: disable=abstract-method
    """Carry ``change_history`` only when the caller asked for it, and record a stated reason.

    Mix in ahead of ``ModelSerializer`` on any serializer whose model uses
    :class:`netix_backend.django.change_history.ChangeHistoryModel`; the field is dropped for every
    request that did not pass ``?include_history=true``, so list payloads stay small.
    """

    change_history = ChangeHistoryEntrySerializer(many=True, read_only=True)
    # Write-only, never a model field — popped in validate() and folded onto the actor for the
    # save, so it lands on the trail entry the write produces.
    change_reason = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
        max_length=MAX_REASON_LENGTH,
        help_text=(
            "Why this change is being made. Recorded as `reason` on the change_history entry "
            "this write produces; not stored as a field of its own."
        ),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Dropped here, not popped after: to_representation would serialize it first, then discard it.
        view = self.context.get("view")
        if getattr(view, "swagger_fake_view", False):
            return
        if not history_requested(self.context.get("request")):
            self.fields.pop("change_history", None)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        attrs = super().validate(attrs)
        # Held on the serializer, not left in validated_data: ModelSerializer would otherwise hand
        # it to Model(**validated_data) and fail on a field the model does not have.
        self._change_reason = attrs.pop(REASON_FIELD, "")
        return attrs

    def save(self, **kwargs: Any) -> Any:
        # getattr, not self._change_reason: save() is reachable without validate() having run.
        reason = getattr(self, "_change_reason", "")
        if not reason:
            return super().save(**kwargs)
        with reason_context(reason):
            return super().save(**kwargs)

    def to_representation(self, instance: Any) -> dict[str, Any]:
        data = super().to_representation(instance)
        if not history_requested(self.context.get("request")):
            data.pop("change_history", None)
        return data
