"""Routed viewsets for the shared-base tests: the DRF test client is the only honest harness for dispatch."""

import json
from typing import Any, ClassVar

from adrf import routers as adrf_routers
from django.urls import include, path
from rest_framework import routers, serializers

from netix_backend.django.excel import BaseExcelViewSet, ExcelExportViewSet
from netix_backend.django.excel_aio import AsyncExcelViewSet
from netix_backend.django.org_scope import SuperuserOrgScopeMixin
from netix_backend.django.org_scope_schema import (
    superuser_org_scope_autoschema,
    superuser_org_scope_schema,
)
from netix_backend.django.views import (
    BaseViewSet,
    CreateListModelMixin,
    IncludeDeletedMixin,
    ListErrorMappingMixin,
    ListErrorRule,
    ObjectOrgOwnership,
    UpdateListModelMixin,
    include_deleted_schema,
)
from netix_backend.django.views_aio import AsyncBaseViewSet
from tests.models import GuardedWidget, PlainWidget, ScopedWidget


class EnvoyHeaderMixin:
    """Attaches the identity the envoy middleware would attach, taken from an X-Test-Envoy header."""

    def initialize_request(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        drf_request = super().initialize_request(request, *args, **kwargs)  # type: ignore[misc]
        raw = request.META.get("HTTP_X_TEST_ENVOY")
        if raw:
            drf_request.envoy = json.loads(raw)
        return drf_request


class ScopedWidgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScopedWidget
        fields = ["id", "label", "organization_id", "is_deleted", "boom"]


class PlainWidgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlainWidget
        fields = ["id", "label", "is_deleted"]


class GuardedWidgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = GuardedWidget
        fields = ["id", "locked", "is_deleted"]


class BoomFilterBackend:
    """Filter backend that fails the way a bad query parameter does."""

    error: ClassVar[Exception] = ValueError("bad query")

    def filter_queryset(self, request: Any, queryset: Any, view: Any) -> Any:
        raise type(self).error

    def get_schema_operation_parameters(self, view: Any) -> list[Any]:
        return []


class WidgetViewSet(EnvoyHeaderMixin, BaseViewSet):
    serializer_class = ScopedWidgetSerializer
    model_queryset = ScopedWidget.objects.all()
    permission_classes: ClassVar[list[Any]] = []


class PinRejectViewSet(WidgetViewSet):
    tenant_write_scope = "pin_and_reject"


class PinBeforeValidationViewSet(WidgetViewSet):
    tenant_write_pin_before_validation = True


class UnpinnedViewSet(WidgetViewSet):
    tenant_write_scope = "off"


class KeepDeletedViewSet(WidgetViewSet):
    """Tombstones stay visible here, so a repeat delete can actually reach the handler."""

    soft_delete_404_on_repeat = False

    def get_queryset(self) -> Any:
        return self._get_queryset_filter(ScopedWidget, delete_filter=False)


class RepeatDeleteViewSet(KeepDeletedViewSet):
    soft_delete_404_on_repeat = True


class NoAtomicViewSet(WidgetViewSet):
    atomic_actions = frozenset()


class GuardedViewSet(EnvoyHeaderMixin, BaseViewSet):
    serializer_class = GuardedWidgetSerializer
    model_queryset = GuardedWidget.objects.all()
    permission_classes: ClassVar[list[Any]] = []
    organization_field_optional = True
    soft_delete_validate = True


class OrglessViewSet(EnvoyHeaderMixin, BaseViewSet):
    serializer_class = PlainWidgetSerializer
    model_queryset = PlainWidget.objects.all()
    permission_classes: ClassVar[list[Any]] = []
    organization_field_optional = True


class ListErrorViewSet(ListErrorMappingMixin, WidgetViewSet):
    filter_backends: ClassVar[list[Any]] = [BoomFilterBackend]
    list_error_map: ClassVar[tuple[ListErrorRule, ...]] = (
        (RuntimeError, lambda error: "out of range" in str(error), "integer out of range"),
        (ValueError, None, "Invalid query"),
    )


@include_deleted_schema()
class IncludeDeletedViewSet(IncludeDeletedMixin, WidgetViewSet):
    model_queryset = None
    include_deleted_live_first = True

    def get_queryset(self) -> Any:
        return self._get_queryset_filter(ScopedWidget)


class IncludeDeletedFallbackViewSet(IncludeDeletedMixin, WidgetViewSet):
    include_deleted_queryset = ScopedWidget.objects.all()


class OrgScopeViewSet(SuperuserOrgScopeMixin, WidgetViewSet):
    """asset/data/viz's shape: the mixin owns get_queryset."""

    superuser_org_scope_model = ScopedWidget


class BlankOrgScopeViewSet(OrgScopeViewSet):
    superuser_org_blank_is_absent = True


class HelperOrgScopeViewSet(SuperuserOrgScopeMixin, WidgetViewSet):
    """cafm's shape: the viewset owns get_queryset and calls the helper explicitly."""

    model_queryset = None

    def get_queryset(self) -> Any:
        return self.get_org_scoped_queryset(ScopedWidget, field_name="organization_id")


# Stands in for an adopter's own prose: the library requires it and never supplies one.
SUPERUSER_ORG_DESCRIPTION = "Read or write another organization's configuration (superusers only)."


@superuser_org_scope_schema(description=SUPERUSER_ORG_DESCRIPTION)
class DecoratedOrgScopeViewSet(OrgScopeViewSet):
    """asset/data's advertising: the extend_schema_view decorator, applied in the repo."""


class AutoSchemaOrgScopeViewSet(OrgScopeViewSet):
    """cafm's advertising: an AutoSchema subclass bound by the repo, never by the mixin."""

    schema = superuser_org_scope_autoschema(description=SUPERUSER_ORG_DESCRIPTION)()


class BulkViewSet(CreateListModelMixin, UpdateListModelMixin, WidgetViewSet):
    pass


class OwnershipLibraryViewSet(WidgetViewSet):
    permission_classes: ClassVar[list[Any]] = [ObjectOrgOwnership]
    tenant_write_scope = "off"
    # Unscoped reads, so a caller can reach another organization's row and be stopped by the object gate.
    session_customer_filter = False


class OwnershipPlainViewSet(EnvoyHeaderMixin, BaseViewSet):
    serializer_class = PlainWidgetSerializer
    model_queryset = PlainWidget.objects.all()
    permission_classes: ClassVar[list[Any]] = [ObjectOrgOwnership]
    tenant_write_scope = "off"
    organization_field_optional = True
    session_customer_filter = False


class OwnershipPlainLegacyViewSet(OwnershipPlainViewSet):
    org_ownership = "legacy_open"


class OwnershipExemptViewSet(OwnershipPlainViewSet):
    org_ownership_exempt_actions = ("partial_update",)


class PermissionViewSet(EnvoyHeaderMixin, BaseViewSet):
    serializer_class = ScopedWidgetSerializer
    model_queryset = ScopedWidget.objects.all()
    permission_module = "widget"
    org_ownership = "off"


class ExcelWidgetViewSet(EnvoyHeaderMixin, BaseExcelViewSet):
    serializer_class = ScopedWidgetSerializer
    model_queryset = ScopedWidget.objects.all()
    permission_classes: ClassVar[list[Any]] = []


class UnscopedExcelViewSet(ExcelExportViewSet):
    """The envoy-free core: every row, whoever asks — user-management scopes its own way."""

    serializer_class = ScopedWidgetSerializer
    queryset = ScopedWidget.objects.all()
    permission_classes: ClassVar[list[Any]] = []


class AsyncExcelWidgetViewSet(AsyncExcelViewSet):
    """user-management's adrf export shape: GET is routed to alist/aretrieve."""

    serializer_class = ScopedWidgetSerializer
    queryset = ScopedWidget.objects.all()
    permission_classes: ClassVar[list[Any]] = []


class AsyncWidgetViewSet(EnvoyHeaderMixin, AsyncBaseViewSet):
    serializer_class = ScopedWidgetSerializer
    model_queryset = ScopedWidget.objects.all()
    permission_classes: ClassVar[list[Any]] = []


class AsyncUnpinnedViewSet(AsyncWidgetViewSet):
    tenant_write_scope = "off"


router = routers.SimpleRouter()
router.register("widgets", WidgetViewSet, basename="widget")
router.register("pin-reject", PinRejectViewSet, basename="pin-reject")
router.register("pin-early", PinBeforeValidationViewSet, basename="pin-early")
router.register("unpinned", UnpinnedViewSet, basename="unpinned")
router.register("keep-deleted", KeepDeletedViewSet, basename="keep-deleted")
router.register("repeat-delete", RepeatDeleteViewSet, basename="repeat-delete")
router.register("no-atomic", NoAtomicViewSet, basename="no-atomic")
router.register("guarded", GuardedViewSet, basename="guarded")
router.register("orgless", OrglessViewSet, basename="orgless")
router.register("list-errors", ListErrorViewSet, basename="list-errors")
router.register("include-deleted", IncludeDeletedViewSet, basename="include-deleted")
router.register("include-deleted-fallback", IncludeDeletedFallbackViewSet, basename="include-deleted-fallback")
router.register("org-scope", OrgScopeViewSet, basename="org-scope")
router.register("org-scope-blank", BlankOrgScopeViewSet, basename="org-scope-blank")
router.register("org-scope-helper", HelperOrgScopeViewSet, basename="org-scope-helper")
router.register("org-scope-decorated", DecoratedOrgScopeViewSet, basename="org-scope-decorated")
router.register("org-scope-autoschema", AutoSchemaOrgScopeViewSet, basename="org-scope-autoschema")
router.register("bulk", BulkViewSet, basename="bulk")
router.register("owned-library", OwnershipLibraryViewSet, basename="owned-library")
router.register("owned-plain", OwnershipPlainViewSet, basename="owned-plain")
router.register("owned-plain-legacy", OwnershipPlainLegacyViewSet, basename="owned-plain-legacy")
router.register("owned-exempt", OwnershipExemptViewSet, basename="owned-exempt")
router.register("gated", PermissionViewSet, basename="gated")
router.register("excel", ExcelWidgetViewSet, basename="excel")
router.register("excel-unscoped", UnscopedExcelViewSet, basename="excel-unscoped")

async_router = adrf_routers.SimpleRouter()
async_router.register("async-widgets", AsyncWidgetViewSet, basename="async-widget")
async_router.register("async-unpinned", AsyncUnpinnedViewSet, basename="async-unpinned")
async_router.register("async-excel", AsyncExcelWidgetViewSet, basename="async-excel")

urlpatterns: list = [
    path("api/", include(router.urls)),
    path("api/", include(async_router.urls)),
]
