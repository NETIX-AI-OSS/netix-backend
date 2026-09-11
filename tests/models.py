"""Concrete models used by the test suite; abstract bases cannot be exercised directly."""

from typing import Any

from django.db import IntegrityError, models
from rest_framework.exceptions import ValidationError

from netix_backend.django.change_history import ChangeHistoryModel
from netix_backend.django.models import (
    BaseModel,
    CloneProvenanceMixin,
    CompactNamedBaseModel,
    NamedBaseModel,
    SluggedNamedBaseModel,
    organization_scoped,
)


class Widget(BaseModel):
    """Plain BaseModel subclass with a unique column, to provoke IntegrityError."""

    code = models.CharField(max_length=50, unique=True)
    note = models.CharField(max_length=50, blank=True, default="")


class GuardedWidget(BaseModel):
    """Blocks its own soft delete, mirroring stormbreaker's Parser."""

    locked = models.BooleanField(default=False)

    def validate_soft_delete(self) -> None:
        if self.locked:
            raise ValidationError("locked")


STATUS_CHOICES = [(1, "PRESENT"), (2, "ABSENT")]


class HistoryWidget(BaseModel, ChangeHistoryModel):
    """The change-trail mixin in the MRO order adopters use: repo base first, mixin second.

    Both bases mutate ``update_fields`` on the way down (``updated_on``, then ``change_history``),
    which is what tests/test_models.py asserts still composes.
    """

    label = models.CharField(max_length=50, blank=True, default="")
    status = models.IntegerField(choices=STATUS_CHOICES, blank=True, null=True)
    owner = models.ForeignKey(Widget, on_delete=models.CASCADE, blank=True, null=True)
    # Deliberately outside history_fields: bookkeeping columns must not produce entries.
    payload = models.CharField(max_length=50, blank=True, default="")

    history_fields = ("label", "status", "owner", "is_deleted")


class CappedHistoryWidget(BaseModel, ChangeHistoryModel):
    """A trail short enough to overflow in a test."""

    counter = models.IntegerField(default=0)

    history_fields = ("counter",)
    history_max_entries = 3


class UntrackedHistoryWidget(BaseModel, ChangeHistoryModel):
    """Declares no ``history_fields``, so the mixin stays inert."""

    label = models.CharField(max_length=50, blank=True, default="")


class LooseHistoryWidget(BaseModel, ChangeHistoryModel):
    """``history_fields`` naming a plain attribute, exercising both field-probe fallbacks."""

    label = models.CharField(max_length=50, blank=True, default="")
    # Not a column: get_field() raises for it, so attname and display both fall back.
    nickname = "unset"

    history_fields = ("nickname", "label")


class LateFailureWidget(BaseModel):
    """Raises after the base save, so an IntegrityError reaches asave's own handler."""

    fail = models.BooleanField(default=False)

    def save(self, *args: Any, **kwargs: Any) -> None:
        super().save(*args, **kwargs)
        if self.fail:
            raise IntegrityError("late failure")


class NamedWidget(NamedBaseModel):
    pass


class SluggedWidget(SluggedNamedBaseModel):
    pass


class CompactWidget(CompactNamedBaseModel):
    pass


class CloneWidget(CloneProvenanceMixin, BaseModel):
    pass


class OrgWidget(organization_scoped(), BaseModel):  # type: ignore[misc]
    pass


class SentinelOrgWidget(organization_scoped(default=-1), BaseModel):  # type: ignore[misc]
    pass


class ScopedWidget(organization_scoped(), BaseModel):  # type: ignore[misc]
    """Organization-scoped row the routed viewset tests read and write."""

    label = models.CharField(max_length=50, blank=True, default="")
    boom = models.BooleanField(default=False)


class PlainWidget(BaseModel):
    """Row with no organization column, for the org-less scoping bypass."""

    label = models.CharField(max_length=50, blank=True, default="")


class RelatedOrgWidget(BaseModel):
    """Organization carried as a ForeignKey, so the field probe must fall back to the relation name."""

    organization = models.ForeignKey(ScopedWidget, on_delete=models.CASCADE, null=True, blank=True)
