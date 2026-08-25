"""adrf counterpart of the xlsx export core, for services whose read routes are async (requires both extras)."""

from __future__ import annotations

from typing import Any

from adrf.viewsets import ReadOnlyModelViewSet as AsyncReadOnlyModelViewSet
from asgiref.sync import async_to_sync, sync_to_async
from drf_excel.mixins import XLSXFileMixin
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from netix_backend.django.excel import ExcelExportMixin
from netix_backend.django.schema import XLSX_BINARY_RESPONSE, XlsxExportAutoSchema

__all__ = ["AsyncExcelViewSet"]


# adrf routes GET to alist/aretrieve, so the response override has to sit on the async handlers.
class AsyncExcelViewSet(ExcelExportMixin, XLSXFileMixin, AsyncReadOnlyModelViewSet):
    schema = XlsxExportAutoSchema()

    @extend_schema(responses={200: XLSX_BINARY_RESPONSE})
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return async_to_sync(self.alist)(request, *args, **kwargs)

    @extend_schema(responses={200: XLSX_BINARY_RESPONSE})
    async def alist(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        # drf-excel's renderer walks the serialized page synchronously, so the whole list runs in one hop.
        response: Response = await sync_to_async(super().list, thread_sensitive=True)(request, *args, **kwargs)
        return response

    @extend_schema(responses={200: XLSX_BINARY_RESPONSE})
    async def aretrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        response: Response = await super().aretrieve(request, *args, **kwargs)
        return response
