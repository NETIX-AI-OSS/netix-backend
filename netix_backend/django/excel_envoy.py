"""The organization-scoped xlsx export viewset, split out so the excel core stays free of the envoy import chain."""

from __future__ import annotations

from netix_backend.django.excel import DocumentedExcelViewSet
from netix_backend.django.views import EnvoyScopedQuerysetMixin

__all__ = ["BaseExcelViewSet", "ScopedExcelViewSet"]


# Envoy organization scoping on top of the documented export core.
class ScopedExcelViewSet(EnvoyScopedQuerysetMixin, DocumentedExcelViewSet):
    pass


# The v1.0.x name, kept as an alias so existing imports and subclasses keep working.
BaseExcelViewSet = ScopedExcelViewSet
