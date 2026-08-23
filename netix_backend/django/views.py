"""Composable viewset mixins and the shared ``BaseViewSet`` the fleet's 15 near-copies converge on."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import cache
from typing import Any, ClassVar, Literal, TypeVar, cast

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist
from django.db import models, transaction
from django.db.models import QuerySet
from django.http import Http404
from envoy_pyauth.permissions import (
    EnvoyActionPermissions,
    EnvoyObjectOrgOwnership,
    HasEnvoy,
    has_permissions,
    require_permissions,
    resolve_required_permission,
)
from envoy_pyauth.utils import EnvoyQueryFilter
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.response import Response

from netix_backend.django.pagination import BaseLimitOffsetPagination

__all__ = [
    "INCLUDE_DELETED_ACTIONS",
    "INCLUDE_DELETED_PARAM",
    "WWW_AUTHENTICATE_HEADER",
    "AtomicWriteMixin",
    "BaseViewSet",
    "CreateListModelMixin",
    "EnvoyActionPermissions",
    "EnvoyPermissionMixin",
    "EnvoyScopedQuerysetMixin",
    "HasEnvoy",
    "IncludeDeletedMixin",
    "ListErrorMappingMixin",
    "ListErrorRule",
    "ObjectOrgOwnership",
    "SoftDeleteMixin",
    "TenantWriteScopeMixin",
    "UpdateListModelMixin",
    "has_permissions",
    "include_deleted_schema",
    "is_superuser",
    "require_permissions",
    "tenant_organization_id",
    "write_permissions",
]

ModelT = TypeVar("ModelT", bound=models.Model)

ScopeMode = Literal["off", "pin", "pin_and_reject"]
OwnershipMode = Literal["library", "legacy_open", "off"]
ListErrorRule = tuple[type[Exception], Callable[[Exception], bool] | None, str]

# DRF coerces NotAuthenticated to 403 when get_authenticate_header returns None.
WWW_AUTHENTICATE_HEADER = 'Bearer realm="api"'

INCLUDE_DELETED_PARAM = "include_deleted"
INCLUDE_DELETED_ACTIONS: tuple[str, ...] = ("list", "retrieve", "update", "partial_update")
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_UNRESOLVED_ORGANIZATIONS = (None, "", "bogus")


@cache
def _accepts_include_shared(entry_point: Any) -> bool:
    """Whether the installed envoy-pyauth exposes ``include_shared`` (v3.0.0) on this entry point."""
    try:
        parameters = inspect.signature(entry_point).parameters
    except (TypeError, ValueError):
        return False
    return "include_shared" in parameters


@cache
def _probe_model_field(model: Hashable, field_name: str) -> bool:
    meta = getattr(model, "_meta", None)
    if meta is None:
        return False
    name = field_name.split("__", 1)[0]
    try:
        meta.get_field(name)
    except FieldDoesNotExist:
        # An ``organization_id`` column may be spelled ``organization`` when it is a ForeignKey.
        if not name.endswith("_id"):
            return False
        try:
            meta.get_field(name[:-3])
        except FieldDoesNotExist:
            return False
    return True


def _model_has_field(model: Any, field_name: str) -> bool:
    """Whether ``model`` carries ``field_name``; a ``__`` path is probed on its first segment only."""
    return _probe_model_field(model, field_name)


def _serializer_model(serializer: Any) -> type[models.Model] | None:
    """The model a serializer (or serializer class) writes to, unwrapping ``many=True``."""
    target = getattr(serializer, "child", serializer)
    meta = getattr(target, "Meta", None)
    model = getattr(meta, "model", None) or getattr(target, "model", None)
    return cast("type[models.Model] | None", model)


def is_superuser(request: Any) -> bool:
    """The fleet's five forked ``is_superuser`` coercions, once: envoy flag first, ``request.user`` second."""
    envoy = getattr(request, "envoy", None)
    if isinstance(envoy, dict):
        value = envoy.get("is_superuser")
        if value is not None:
            if isinstance(value, str):
                return value.strip().lower() == "true"
            return bool(value)
    user = getattr(request, "user", None)
    return bool(user and getattr(user, "is_authenticated", False) and getattr(user, "is_superuser", False))


def tenant_organization_id(request: Any) -> int | None:
    """The organization a caller must write into; ``None`` for platform, superuser and unresolved callers."""
    envoy = getattr(request, "envoy", None)
    if not isinstance(envoy, dict) or is_superuser(request):
        return None
    caller = envoy.get("organization")
    if caller in _UNRESOLVED_ORGANIZATIONS:
        return None
    try:
        organization_id = int(caller)
    except (TypeError, ValueError) as error:
        raise PermissionDenied("The authenticated organization is invalid.") from error
    return None if organization_id == 0 else organization_id


def write_permissions(codename: str) -> dict[str, str]:
    """A ``required_permissions`` map gating every CRUD write action on ``codename``."""
    return dict.fromkeys(("create", "update", "partial_update", "destroy"), codename)


# Envoy organization scoping behind one keyword-only signature covering all five in-tree shapes.
class EnvoyScopedQuerysetMixin:
    request: Any

    organization_field: ClassVar[str] = "organization_id"
    session_customer_filter: ClassVar[bool | None] = None
    model_queryset: ClassVar[QuerySet[Any] | None] = None
    # Opt-in: leave models that have no organization column unscoped instead of letting the filter raise.
    organization_field_optional: ClassVar[bool] = False

    def resolve_session_customer_filter(self, model: Any = None, field_name: str | None = None) -> bool:
        """``session_customer_filter=None`` resolves lazily from settings, defaulting to scoped."""
        value = self.session_customer_filter
        if value is None:
            value = getattr(settings, "SESSION_CUSTOMER_FILTER", True)
        if value and self.organization_field_optional and model is not None:
            return _model_has_field(model, field_name or self.organization_field)
        return bool(value)

    def _scope_call_kwargs(
        self,
        entry_point: Any,
        model: Any,
        *,
        field_name: str | None,
        delete_filter: bool,
        include_shared: bool | None,
    ) -> dict[str, Any]:
        field = field_name or self.organization_field
        kwargs: dict[str, Any] = {
            "request": getattr(self, "request", None),
            "session_customer_filter": self.resolve_session_customer_filter(model, field),
            "field_name": field,
            "delete_filter": delete_filter,
        }
        # v2.0.0 pins have no include_shared parameter; forwarding it there is a TypeError.
        if include_shared is not None and _accepts_include_shared(entry_point):
            kwargs["include_shared"] = include_shared
        return kwargs

    def _get_queryset_filter(
        self,
        model: type[ModelT],
        *,
        field_name: str | None = None,
        delete_filter: bool = True,
        include_shared: bool | None = None,
        additional_filter: Mapping[str, Any] | None = None,
    ) -> QuerySet[ModelT]:
        """Organization-scoped queryset for ``model``; every knob is keyword-only so library inserts stay safe."""
        entry_point = EnvoyQueryFilter.get_queryset
        kwargs = self._scope_call_kwargs(
            entry_point,
            model,
            field_name=field_name,
            delete_filter=delete_filter,
            include_shared=include_shared,
        )
        queryset = cast("QuerySet[ModelT]", entry_point(model=model, **kwargs))
        if additional_filter:
            queryset = queryset.filter(**additional_filter)
        return queryset

    def _scope_queryset(
        self,
        queryset: QuerySet[ModelT],
        *,
        field_name: str | None = None,
        delete_filter: bool = True,
        include_shared: bool | None = None,
        additional_filter: Mapping[str, Any] | None = None,
    ) -> QuerySet[ModelT]:
        """Scope an already-built queryset, for viewsets that annotate or join before scoping."""
        entry_point = EnvoyQueryFilter.filter_queryset
        kwargs = self._scope_call_kwargs(
            entry_point,
            queryset.model,
            field_name=field_name,
            delete_filter=delete_filter,
            include_shared=include_shared,
        )
        scoped = cast("QuerySet[ModelT]", entry_point(queryset=queryset, **kwargs))
        if additional_filter:
            scoped = scoped.filter(**additional_filter)
        return scoped

    def get_queryset(self) -> QuerySet[Any]:
        """Scopes ``model_queryset`` when declared; subclasses adding logic override and call ``super()``."""
        model_queryset = self.model_queryset
        if model_queryset is None:
            return cast("QuerySet[Any]", super().get_queryset())  # type: ignore[misc]
        if getattr(self, "request", None) is None:
            # drf-spectacular introspects the view with no request bound.
            return model_queryset.none()
        # .all() clones so the class-level queryset's result cache is never shared across requests.
        return self._scope_queryset(model_queryset.all())


# Write-time organization pinning: the fleet's six ad-hoc ``perform_create`` forks as one knob.
class TenantWriteScopeMixin:
    request: Any

    tenant_write_scope: ClassVar[ScopeMode] = "pin"
    tenant_write_scope_field: ClassVar[str] = "organization_id"
    tenant_write_exempt_actions: ClassVar[frozenset[str]] = frozenset()
    # stormbreaker pins inside get_serializer so validators derive organization-specific values.
    tenant_write_pin_before_validation: ClassVar[bool] = False

    def tenant_organization_id(self) -> int | None:
        """The organization to force onto this write, or ``None`` to leave the body alone."""
        if self.tenant_write_scope == "off":
            return None
        if getattr(self, "action", None) in self.tenant_write_exempt_actions:
            return None
        return tenant_organization_id(getattr(self, "request", None))

    def tenant_save_kwargs(self, serializer: Any) -> dict[str, int]:
        """``serializer.save()`` kwargs pinning the caller's organization; empty when the model has no column."""
        organization_id = self.tenant_organization_id()
        if organization_id is None:
            return {}
        model = _serializer_model(serializer)
        if model is None or not _model_has_field(model, self.tenant_write_scope_field):
            return {}
        return {self.tenant_write_scope_field: organization_id}

    def _reject_foreign_body(self, serializer: Any, organization_id: int) -> None:
        validated = getattr(serializer, "validated_data", None) or {}
        rows = validated if isinstance(validated, list) else [validated]
        for row in rows:
            submitted = row.get(self.tenant_write_scope_field)
            if submitted is None:
                continue
            if str(submitted) != str(organization_id):
                raise PermissionDenied("Cannot write records for another organization.")

    def _reject_foreign_instance(self, serializer: Any, organization_id: int) -> None:
        instance = getattr(serializer, "instance", None)
        owner = getattr(instance, self.tenant_write_scope_field, None)
        if owner is not None and str(owner) != str(organization_id):
            raise PermissionDenied("Tenant identities may only modify their organization's records.")

    def perform_create(self, serializer: Any) -> None:
        save_kwargs = self.tenant_save_kwargs(serializer)
        if not save_kwargs:
            super().perform_create(serializer)  # type: ignore[misc]
            return
        if self.tenant_write_scope == "pin_and_reject":
            self._reject_foreign_body(serializer, save_kwargs[self.tenant_write_scope_field])
        serializer.save(**save_kwargs)

    def perform_update(self, serializer: Any) -> None:
        save_kwargs = self.tenant_save_kwargs(serializer)
        if not save_kwargs:
            super().perform_update(serializer)  # type: ignore[misc]
            return
        organization_id = save_kwargs[self.tenant_write_scope_field]
        if self.tenant_write_scope == "pin_and_reject":
            self._reject_foreign_body(serializer, organization_id)
            self._reject_foreign_instance(serializer, organization_id)
        serializer.save(**save_kwargs)

    def get_serializer(self, *args: Any, **kwargs: Any) -> Any:
        data = kwargs.get("data")
        if data is not None and self.tenant_write_pin_before_validation:
            kwargs["data"] = self._pinned_write_data(data)
        return super().get_serializer(*args, **kwargs)  # type: ignore[misc]

    def _pinned_write_data(self, data: Any) -> Any:
        organization_id = self.tenant_organization_id()
        if organization_id is None or isinstance(data, list):
            return data
        model = _serializer_model(self.get_serializer_class())  # type: ignore[attr-defined]
        if model is None or not _model_has_field(model, self.tenant_write_scope_field):
            return data
        pinned = data.copy()
        pinned[self.tenant_write_scope_field] = organization_id
        return pinned


# ``destroy`` flags the row instead of deleting it, atomically and with one 404 policy.
class SoftDeleteMixin:
    soft_delete_field: ClassVar[str] = "is_deleted"
    soft_delete_404_on_repeat: ClassVar[bool] = True
    # stormbreaker's Parser blocks its own retirement; off by default so adoption changes no response codes.
    soft_delete_validate: ClassVar[bool] = False

    def soft_delete_instance(self, instance: Any) -> None:
        """Retire one row, delegating to the model's own ``soft_delete`` when it has one."""
        if self.soft_delete_404_on_repeat and getattr(instance, self.soft_delete_field, False):
            raise Http404
        if self.soft_delete_validate:
            validate = getattr(instance, "validate_soft_delete", None)
            if callable(validate):
                validate()
        model_soft_delete = getattr(instance, "soft_delete", None)
        if callable(model_soft_delete):
            model_soft_delete()
            return
        setattr(instance, self.soft_delete_field, True)
        instance.save(update_fields=self._soft_delete_update_fields(instance))

    def _soft_delete_update_fields(self, instance: Any) -> list[str]:
        fields = [self.soft_delete_field]
        if _model_has_field(type(instance), "updated_on"):
            fields.append("updated_on")
        return fields

    @transaction.atomic
    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        try:
            instance = self.get_object()  # type: ignore[attr-defined]
            self.soft_delete_instance(instance)
        except Http404:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)


# Atomicity as a knob: the resolved write handler runs inside ``transaction.atomic``.
class AtomicWriteMixin:
    atomic_actions: ClassVar[frozenset[str]] = frozenset({"create", "update", "partial_update", "destroy"})

    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        method = (request.method or "").lower()
        action = (getattr(self, "action_map", None) or {}).get(method)
        handler = getattr(self, method, None)
        # Async handlers are wrapped by the aio module instead; transaction.atomic cannot wrap a coroutine.
        if action in self.atomic_actions and handler is not None and not inspect.iscoroutinefunction(handler):
            setattr(self, method, transaction.atomic(handler))
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc]


def _legacy_object_org_permission(request: Any, obj: Any) -> bool:
    """envoy-pyauth v1.6 semantics: no identity allows, an unowned row allows, orgs compare as strings."""
    if request.method in SAFE_METHODS:
        return True
    envoy = getattr(request, "envoy", None)
    if not envoy:
        return True
    caller = envoy.get("organization")
    if str(caller) == "0" or str(envoy.get("is_superuser")).lower() == "true":
        return True
    owner = getattr(obj, "organization_id", None)
    return owner is None or str(owner) == str(caller)


class ObjectOrgOwnership(BasePermission):
    """Object-level organization gate whose strictness the view picks via ``org_ownership``."""

    message = "This record belongs to another organization."

    def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
        mode = getattr(view, "org_ownership", "library")
        if mode == "off":
            return True
        if getattr(view, "action", None) in getattr(view, "org_ownership_exempt_actions", ()):
            return True
        if mode == "legacy_open":
            return _legacy_object_org_permission(request, obj)
        return bool(EnvoyObjectOrgOwnership().has_object_permission(request, view, obj))


# Per-action codename gating plus the object-level organization guard.
class EnvoyPermissionMixin:
    permission_classes: Sequence[Any] = [EnvoyActionPermissions, ObjectOrgOwnership]
    permission_module: ClassVar[str | None] = None
    required_permissions: ClassVar[Mapping[str, str]] = {}
    org_ownership: ClassVar[OwnershipMode] = "library"
    org_ownership_exempt_actions: ClassVar[tuple[str, ...]] = ()

    # None keeps DRF's default (NotAuthenticated coerced to 403); set WWW_AUTHENTICATE_HEADER to get real 401s.
    authenticate_header: ClassVar[str | None] = None

    def get_required_permission(self) -> str | None:
        # envoy_pyauth ships no py.typed marker.
        return cast("str | None", resolve_required_permission(self))

    # drf-stubs types the return as str, but DRF's own default implementation returns None.
    def get_authenticate_header(self, request: Request) -> str:
        return cast("str", self.authenticate_header)


# Turns declared queryset errors into 400s instead of 500s, for list filtering and pagination.
class ListErrorMappingMixin:
    list_error_map: ClassVar[tuple[ListErrorRule, ...]] = ((ValueError, None, "Invalid query"),)

    @contextmanager
    def _mapped_list_errors(self) -> Iterator[None]:
        try:
            yield
        except Exception as error:
            for error_type, predicate, message in self.list_error_map:
                if isinstance(error, error_type) and (predicate is None or predicate(error)):
                    raise ValidationError(message) from error
            raise

    def filter_queryset(self, queryset: QuerySet[Any]) -> QuerySet[Any]:
        with self._mapped_list_errors():
            return cast("QuerySet[Any]", super().filter_queryset(queryset))  # type: ignore[misc]

    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        with self._mapped_list_errors():
            return cast("Response", super().list(request, *args, **kwargs))  # type: ignore[misc]


# Create a list of instances when the body is a list, a single instance otherwise.
class CreateListModelMixin:
    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        viewset = cast("viewsets.ModelViewSet[Any]", self)
        serializer = viewset.get_serializer(data=request.data, many=isinstance(request.data, list))
        serializer.is_valid(raise_exception=True)
        viewset.perform_create(serializer)
        headers = viewset.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)


# Update a list of instances when the body is a list, a single instance otherwise.
class UpdateListModelMixin:
    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        viewset = cast("viewsets.ModelViewSet[Any]", self)
        if isinstance(request.data, list):
            serializer = viewset.get_serializer(data=request.data, many=True)
        else:
            partial = kwargs.pop("partial", False)
            instance = viewset.get_object()
            serializer = viewset.get_serializer(instance, data=request.data, partial=partial)
            if getattr(instance, "_prefetched_objects_cache", None):
                # prefetch_related was applied; the cache must not survive the write.
                instance._prefetched_objects_cache = {}
        serializer.is_valid(raise_exception=True)
        viewset.perform_update(serializer)
        return Response(serializer.data)


# Opt-in ``?include_deleted=true``, dropping only the ``is_deleted`` predicate.
class IncludeDeletedMixin:
    request: Any

    include_deleted_actions: ClassVar[tuple[str, ...]] = INCLUDE_DELETED_ACTIONS
    # Fallback for viewsets with no _get_queryset_filter: the same queryset minus the is_deleted predicate.
    include_deleted_queryset: ClassVar[QuerySet[Any] | None] = None
    include_deleted_live_first: ClassVar[bool] = False

    def include_deleted_requested(self) -> bool:
        if getattr(self, "action", None) not in self.include_deleted_actions:
            return False
        request = getattr(self, "request", None)
        if request is None:
            return False
        raw = request.query_params.get(INCLUDE_DELETED_PARAM)
        return raw is not None and str(raw).strip().lower() in _TRUTHY

    def _get_queryset_filter(self, model: Any, *, delete_filter: bool = True, **kwargs: Any) -> QuerySet[Any]:
        return cast(
            "QuerySet[Any]",
            super()._get_queryset_filter(  # type: ignore[misc]
                model,
                delete_filter=delete_filter and not self.include_deleted_requested(),
                **kwargs,
            ),
        )

    def get_queryset(self) -> QuerySet[Any]:
        include_deleted_queryset = self.include_deleted_queryset
        if include_deleted_queryset is None or not self.include_deleted_requested():
            return cast("QuerySet[Any]", super().get_queryset())  # type: ignore[misc]
        scope = getattr(self, "_scope_queryset", None)
        if scope is None:
            return include_deleted_queryset.all()
        return cast("QuerySet[Any]", scope(include_deleted_queryset.all(), delete_filter=False))

    def filter_queryset(self, queryset: QuerySet[Any]) -> QuerySet[Any]:
        filtered = cast("QuerySet[Any]", super().filter_queryset(queryset))  # type: ignore[misc]
        if not (self.include_deleted_live_first and self.include_deleted_requested()):
            return filtered
        # Live row before its tombstone namesake, so a caller reconciling a natural key adopts the live one.
        return filtered.order_by("is_deleted", *(filtered.query.order_by or ("id",)))


def include_deleted_schema(*actions: str) -> Any:
    """Class decorator documenting ``?include_deleted`` on the actions that honour it."""
    from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view

    parameter = OpenApiParameter(
        name=INCLUDE_DELETED_PARAM,
        type=bool,
        location=OpenApiParameter.QUERY,
        required=False,
        default=False,
        description=(
            "Include soft-deleted rows. Honoured on the read actions and on the object lookup of "
            'update/partial_update, so a retired row can be restored with PATCH {"is_deleted": false}. '
            "Organization scoping is unchanged."
        ),
    )
    decorated = extend_schema(parameters=[parameter])
    return extend_schema_view(**dict.fromkeys(actions or INCLUDE_DELETED_ACTIONS, decorated))


# Scoped reads, pinned writes, atomic CRUD and soft delete; filter backends stay per-repo.
class BaseViewSet(
    EnvoyPermissionMixin,
    EnvoyScopedQuerysetMixin,
    TenantWriteScopeMixin,
    AtomicWriteMixin,
    SoftDeleteMixin,
    viewsets.ModelViewSet[Any],
):
    pagination_class = BaseLimitOffsetPagination
