"""adrf counterparts of the shared viewsets; kept in its own module so views.py never imports adrf."""

from __future__ import annotations

from typing import Any, ClassVar

from adrf import viewsets as adrf_viewsets
from asgiref.sync import sync_to_async
from django.db import transaction
from django.db.models import QuerySet
from django.http import Http404
from rest_framework import mixins, status
from rest_framework.request import Request
from rest_framework.response import Response

from netix_backend.django.pagination import BaseLimitOffsetPagination
from netix_backend.django.views import (
    AtomicWriteMixin,
    EnvoyPermissionMixin,
    EnvoyScopedQuerysetMixin,
    SoftDeleteMixin,
    TenantWriteScopeMixin,
)

__all__ = ["AsyncBaseViewSet", "AsyncSoftDeleteMixin"]

# A hook still resolving to one of these is not a subclass override, so the async path may save natively.
_CREATE_BASELINES = (mixins.CreateModelMixin.perform_create, TenantWriteScopeMixin.perform_create)
_UPDATE_BASELINES = (mixins.UpdateModelMixin.perform_update, TenantWriteScopeMixin.perform_update)


# adrf-routed soft delete with the same 404/validate policy as the sync handler.
class AsyncSoftDeleteMixin(SoftDeleteMixin):
    async def adestroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        try:
            instance = await self.aget_object()  # type: ignore[attr-defined]
            await sync_to_async(self._atomic_soft_delete, thread_sensitive=True)(instance)
        except Http404:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _atomic_soft_delete(self, instance: Any) -> None:
        # transaction.atomic is sync-only, so the whole soft delete runs in one thread-sensitive hop.
        with transaction.atomic():
            self.soft_delete_instance(instance)


class AsyncBaseViewSet(
    EnvoyPermissionMixin,
    EnvoyScopedQuerysetMixin,
    TenantWriteScopeMixin,
    AtomicWriteMixin,
    AsyncSoftDeleteMixin,
    adrf_viewsets.ModelViewSet,
):
    """The async base: same scoping and pinning as BaseViewSet, honouring sync write hooks when overridden."""

    pagination_class: ClassVar[Any] = BaseLimitOffsetPagination

    async def _aget_queryset_filter(self, model: Any, **kwargs: Any) -> QuerySet[Any]:
        return await sync_to_async(self._get_queryset_filter, thread_sensitive=True)(model, **kwargs)

    def _has_sync_override(self, hook_name: str, *baselines: Any) -> bool:
        return getattr(type(self), hook_name, None) not in baselines

    async def _asave(self, serializer: Any) -> None:
        if hasattr(serializer, "asave"):
            await serializer.asave()
        else:
            await sync_to_async(serializer.save, thread_sensitive=True)()

    async def perform_acreate(self, serializer: Any) -> None:
        if self._has_sync_override("perform_create", *_CREATE_BASELINES) or self.tenant_save_kwargs(serializer):
            await sync_to_async(self.perform_create, thread_sensitive=True)(serializer)
            return
        await self._asave(serializer)

    async def perform_aupdate(self, serializer: Any) -> None:
        if self._has_sync_override("perform_update", *_UPDATE_BASELINES) or self.tenant_save_kwargs(serializer):
            await sync_to_async(self.perform_update, thread_sensitive=True)(serializer)
            return
        await self._asave(serializer)
