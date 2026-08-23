"""Shared abstract models: timestamps, soft delete, and the NETIX naming shapes."""

from collections.abc import Iterable
from typing import Any, Self

from asgiref.sync import sync_to_async
from django.db import IntegrityError, models
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError

__all__ = [
    "BaseManager",
    "BaseModel",
    "BaseQuerySet",
    "CloneProvenanceMixin",
    "CompactNamedBaseModel",
    "NamedBaseModel",
    "SluggedNamedBaseModel",
    "organization_scoped",
]


class BaseQuerySet(models.QuerySet[Any]):
    """Soft-delete aware queryset with a bulk_update that keeps ``updated_on`` honest."""

    def alive(self) -> Self:
        return self.filter(is_deleted=False)

    def dead(self) -> Self:
        return self.filter(is_deleted=True)

    def stamp_and_update(
        self,
        objs: Iterable[Any],
        fields: Iterable[str],
        *,
        batch_size: int | None = None,
    ) -> int:
        """bulk_update that stamps ``updated_on`` by hand — bulk_update never runs ``auto_now``."""
        rows = list(objs)
        now = timezone.now()
        for row in rows:
            row.updated_on = now
        stamped = list(dict.fromkeys([*fields, "updated_on"]))
        return self.bulk_update(rows, stamped, batch_size=batch_size)


class BaseManager(models.Manager.from_queryset(BaseQuerySet)):  # type: ignore[misc]
    """Default manager for :class:`BaseModel`; adds the cafm ``get_or_none`` contract."""

    # keep False so adding this manager to an abstract base emits no migration operations
    use_in_migrations = False

    def get_or_none(self, **kwargs: Any) -> Any | None:
        try:
            return self.get(**kwargs)
        except self.model.DoesNotExist:
            return None

    async def aget_or_none(self, **kwargs: Any) -> Any | None:
        try:
            return await self.aget(**kwargs)
        except self.model.DoesNotExist:
            return None


class BaseModel(models.Model):
    """Timestamped, soft-deletable base. Never make it concrete: cafm re-declares ``created_on`` on subclasses."""

    class Meta:
        abstract = True

    id: Any  # Auto-generated primary key

    created_on = models.DateTimeField(auto_now_add=True)
    updated_on = models.DateTimeField(auto_now=True)
    is_deleted = models.BooleanField(default=False)

    objects = BaseManager()

    @staticmethod
    def _ensure_updated_on(kwargs: dict[str, Any]) -> None:
        """A partial write must still bump ``updated_on``; auto_now is skipped when update_fields excludes it."""
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = list({*update_fields, "updated_on"})

    def save(self, *args: Any, **kwargs: Any) -> None:
        self._ensure_updated_on(kwargs)
        try:
            super().save(*args, **kwargs)
        except IntegrityError as error:
            raise ValidationError(str(error)) from error

    async def asave(self, *args: Any, **kwargs: Any) -> None:
        self._ensure_updated_on(kwargs)
        try:
            await super().asave(*args, **kwargs)
        except IntegrityError as error:
            raise ValidationError(str(error)) from error

    def validate_soft_delete(self) -> None:
        """Hook: raise to block a soft delete. No-op by default."""
        return None

    def soft_delete(self, *, using: str | None = None) -> None:
        self.validate_soft_delete()
        self.is_deleted = True
        self.save(using=using, update_fields=["is_deleted"])

    async def asoft_delete(self, *, using: str | None = None) -> None:
        # overrides of validate_soft_delete query the ORM synchronously, so it cannot be awaited directly
        await sync_to_async(self.validate_soft_delete)()
        self.is_deleted = True
        await self.asave(using=using, update_fields=["is_deleted"])

    def __str__(self) -> str:
        return f"{self.pk}"

    def __repr__(self) -> str:
        return f"{type(self).__name__} - {self.pk}"


class NamedBaseModel(BaseModel):
    """The 14-repo canonical named shape."""

    class Meta:
        abstract = True

    name = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255)
    is_archived = models.BooleanField(default=False)
    description = models.CharField(max_length=255, blank=True, null=True, default=None)

    def __str__(self) -> str:
        return f"{self.pk} - {self.name}"

    def __repr__(self) -> str:
        return f"{type(self).__name__} - {self.pk} - {self.name}"


class SluggedNamedBaseModel(BaseModel):
    """data-service / stormbreaker shape: slugified name, and deliberately no ``is_archived``."""

    class Meta:
        abstract = True

    name = models.CharField(max_length=255, default=None)
    display_name = models.CharField(max_length=255, default=None)
    description = models.CharField(max_length=255, blank=True, null=True, default=None)

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self.name:
            self.name = slugify(self.display_name)
        if not self.display_name:
            self.display_name = self.name
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.pk} - {self.name}"

    def __repr__(self) -> str:
        return f"{type(self).__name__} - {self.pk} - {self.name}"


class CompactNamedBaseModel(BaseModel):
    """user-management shape: 256-wide name/description, no display_name, no is_archived."""

    class Meta:
        abstract = True

    name = models.CharField(max_length=256)
    description = models.CharField(max_length=256, blank=True, null=True, default=None)

    def __str__(self) -> str:
        return f"{self.pk} - {self.name}"

    def __repr__(self) -> str:
        return f"{type(self).__name__} - {self.pk} - {self.name}"


class CloneProvenanceMixin(models.Model):
    """Where a cloned primitive came from, and whether the tenant has changed it since.

    Every organization gets its own copy of the org-0 template catalog. Provenance is what makes the
    copies maintainable: an addition can propagate to every org, an edit only to clones nobody has
    customized, and a deletion never propagates at all.
    """

    class Meta:
        abstract = True

    # a plain integer, not a ForeignKey: a cascade from a template row onto live tenant rows is the failure to avoid
    cloned_from_id = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Id of the org-0 template row this was cloned from; null for rows created by hand.",
    )
    template_revision = models.IntegerField(
        default=0,
        help_text="Revision of the template at clone time, so drift against a newer template is detectable.",
    )
    is_customized = models.BooleanField(
        default=False,
        help_text=(
            "Set once the owning organization edits the clone. Template updates skip customized "
            "rows rather than overwriting a tenant's deliberate change."
        ),
    )

    def mark_customized(self) -> None:
        """Record that a tenant has diverged this clone from its template."""
        if self.cloned_from_id and not self.is_customized:
            self.is_customized = True


def organization_scoped(*, default: int = 0) -> type[models.Model]:
    """Abstract model carrying ``organization_id``; the default is per-repo (data-service 0, stormbreaker -1)."""

    class OrganizationScoped(models.Model):
        class Meta:
            abstract = True

        organization_id = models.IntegerField(null=False, blank=False, default=default)

    return OrganizationScoped
