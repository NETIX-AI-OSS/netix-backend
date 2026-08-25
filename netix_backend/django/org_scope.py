"""Cross-organization reads and writes for platform superusers, via ``?organization=<id>``.

Runtime only. The mixin never defines ``schema``, never imports drf-spectacular and never emits an
OpenAPI parameter: four repos advertise this parameter through three different mechanisms and one of
them is not on drf-spectacular at all, so advertising stays with the adopter. Import the helpers in
:mod:`netix_backend.django.org_scope_schema` to document it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar, cast

from django.db.models import QuerySet
from rest_framework.exceptions import PermissionDenied, ValidationError

from netix_backend.django.views import _serializer_model, is_superuser

__all__ = [
    "FORBIDDEN_ORGANIZATION",
    "INVALID_ORGANIZATION",
    "ORGANIZATION_QUERY_PARAM",
    "SuperuserOrgScopeMixin",
]

ORGANIZATION_QUERY_PARAM = "organization"
INVALID_ORGANIZATION = "Must be an integer organization id."
FORBIDDEN_ORGANIZATION = "Only superusers can access another organization's configuration."


# No class docstring on purpose: a docstring anywhere in a view's MRO becomes every operation's
# OpenAPI description. See tests/test_schema.py.
class SuperuserOrgScopeMixin:
    request: Any

    superuser_org_query_param: ClassVar[str] = ORGANIZATION_QUERY_PARAM
    superuser_org_scope_field: ClassVar[str] = "organization_id"
    superuser_org_scope_model: ClassVar[Any] = None
    # cafm/viz read a blank ``?organization=`` as absent; asset/data answer it with a 400.
    superuser_org_blank_is_absent: ClassVar[bool] = False
    superuser_org_invalid_message: ClassVar[str] = INVALID_ORGANIZATION
    superuser_org_forbidden_message: ClassVar[str] = FORBIDDEN_ORGANIZATION
    superuser_org_ordering: ClassVar[tuple[str, ...]] = ("id",)
    # Load-bearing: BaseManager.get_queryset does not filter soft-deleted rows.
    superuser_org_extra_filter: ClassVar[Mapping[str, Any]] = {"is_deleted": False}

    def superuser_target_organization_id(self) -> int | None:
        """The requested cross-org target, ``None`` for normal scoping; 400 on a non-integer, 403 for a tenant."""
        request = getattr(self, "request", None)
        if request is None:
            # drf-spectacular introspects the view with no request bound.
            return None
        raw = request.query_params.get(self.superuser_org_query_param)
        if raw is None or (self.superuser_org_blank_is_absent and raw == ""):
            return None
        try:
            organization_id = int(raw)
        except (TypeError, ValueError) as error:
            raise ValidationError({self.superuser_org_query_param: self.superuser_org_invalid_message}) from error
        # Parse before the gate: a tenant sending garbage gets the 400, not the 403.
        if not is_superuser(request):
            raise PermissionDenied(self.superuser_org_forbidden_message)
        return organization_id

    def superuser_org_manager(self, model: Any) -> Any:
        """The manager the cross-org read goes through; ``_default_manager`` honours a model's ``Meta``."""
        return model._default_manager

    def superuser_org_model(self) -> Any:
        """The model the cross-org branch reads, from the explicit knob or the view's own declarations."""
        model = self.superuser_org_scope_model
        if model is not None:
            return model
        for queryset in (getattr(self, "model_queryset", None), getattr(self, "queryset", None)):
            if queryset is not None:
                return queryset.model
        model = _serializer_model(getattr(self, "serializer_class", None))
        if model is None:
            raise RuntimeError("SuperuserOrgScopeMixin requires 'superuser_org_scope_model' to be set.")
        return model

    def superuser_org_queryset(self, model: Any = None, *, organization_id: int) -> QuerySet[Any]:
        """The cross-org branch itself: the target organization's live rows, with no permission logic."""
        manager = self.superuser_org_manager(self.superuser_org_model() if model is None else model)
        filters = {self.superuser_org_scope_field: organization_id, **self.superuser_org_extra_filter}
        return cast("QuerySet[Any]", manager.filter(**filters).order_by(*self.superuser_org_ordering))

    def get_org_scoped_queryset(self, model: Any = None, **scope_kwargs: Any) -> QuerySet[Any]:
        """Cross-org when targeted, else the envoy-scoped queryset; for viewsets that own their ``get_queryset``."""
        organization_id = self.superuser_target_organization_id()
        if organization_id is not None:
            return self.superuser_org_queryset(model, organization_id=organization_id)
        resolved = self.superuser_org_model() if model is None else model
        return cast("QuerySet[Any]", self._get_queryset_filter(resolved, **scope_kwargs))  # type: ignore[attr-defined]

    def get_queryset(self) -> QuerySet[Any]:
        """Cross-org when targeted, else whatever the rest of the MRO would have returned."""
        organization_id = self.superuser_target_organization_id()
        if organization_id is None:
            return cast("QuerySet[Any]", super().get_queryset())  # type: ignore[misc]
        return self.superuser_org_queryset(organization_id=organization_id)

    # The targeted branch calls serializer.save directly instead of super(): the query parameter is
    # meant to win over the body, so a ``tenant_write_scope="pin_and_reject"`` below this mixin never
    # sees the cross-org write and never rejects it. Adopters inherit that asymmetry deliberately.
    def perform_create(self, serializer: Any) -> Any:
        organization_id = self.superuser_target_organization_id()
        if organization_id is None:
            return super().perform_create(serializer)  # type: ignore[misc]
        return serializer.save(**{self.superuser_org_scope_field: organization_id})

    def perform_update(self, serializer: Any) -> Any:
        organization_id = self.superuser_target_organization_id()
        if organization_id is None:
            return super().perform_update(serializer)  # type: ignore[misc]
        return serializer.save(**{self.superuser_org_scope_field: organization_id})
