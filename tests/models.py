"""Concrete models used by the test suite; abstract bases cannot be exercised directly."""

from typing import Any

from django.db import IntegrityError, models
from rest_framework.exceptions import ValidationError

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


class HistoryWidget(BaseModel):
    """Subclass that also mutates ``update_fields``, mirroring user-management's ChangeHistoryModel."""

    change_history = models.CharField(max_length=50, blank=True, default="")
    payload = models.CharField(max_length=50, blank=True, default="")

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = [*update_fields, "change_history"]
        super().save(*args, **kwargs)


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
