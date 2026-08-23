"""Shared XLSX export viewset: two style presets and the binary response schema, applied unconditionally."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, ClassVar, Final

from drf_excel.mixins import XLSXFileMixin
from drf_excel.renderers import XLSXRenderer
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from netix_backend.django.pagination import BigLimitOffsetPagination
from netix_backend.django.schema import XLSX_BINARY_RESPONSE, XlsxExportAutoSchema
from netix_backend.django.views import EnvoyScopedQuerysetMixin

__all__ = [
    "EXCEL_STYLE_APTOS",
    "EXCEL_STYLE_ARIAL",
    "WRAP_ALIGNMENT",
    "BaseExcelViewSet",
    "excel_style",
]

_BORDER_SIDE: Final[dict[str, Any]] = {"border_style": "thin", "color": "FF000000"}

WRAP_ALIGNMENT: Final[dict[str, Any]] = {
    "horizontal": "left",
    "vertical": "center",
    "wrapText": True,
    "shrink_to_fit": True,
}


def _preset(font: str, header_size: int, body_size: int) -> dict[str, Any]:
    return {
        "column_header": {
            "height": 25,
            "style": {
                "border_side": dict(_BORDER_SIDE),
                "font": {"name": font, "size": header_size, "bold": True, "color": "FF000000"},
            },
        },
        "body": {
            "height": 40,
            "style": {
                "border_side": dict(_BORDER_SIDE),
                "font": {"name": font, "size": body_size, "bold": False, "color": "FF000000"},
            },
        },
    }


EXCEL_STYLE_ARIAL: Final[dict[str, Any]] = _preset("Arial", 14, 14)
EXCEL_STYLE_APTOS: Final[dict[str, Any]] = _preset("Aptos Narrow", 14, 11)


def excel_style(preset: Mapping[str, Any], *, wrap: bool = False) -> dict[str, Any]:
    """A private copy of a preset; ``wrap=True`` adds cafm's alignment block to the body style."""
    style = copy.deepcopy(dict(preset))
    if wrap:
        style["body"]["style"]["alignment"] = dict(WRAP_ALIGNMENT)
    return style


_DEFAULT_STYLE = excel_style(EXCEL_STYLE_ARIAL)


class BaseExcelViewSet(EnvoyScopedQuerysetMixin, XLSXFileMixin, ReadOnlyModelViewSet[Any]):
    """Read-only xlsx export. The binary response schema ships here so no repo can forget it."""

    schema = XlsxExportAutoSchema()
    renderer_classes = (XLSXRenderer,)
    filename = "export.xlsx"
    pagination_class = BigLimitOffsetPagination
    column_header: ClassVar[dict[str, Any]] = _DEFAULT_STYLE["column_header"]
    body: ClassVar[dict[str, Any]] = _DEFAULT_STYLE["body"]

    @extend_schema(responses={200: XLSX_BINARY_RESPONSE})
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(responses={200: XLSX_BINARY_RESPONSE})
    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().retrieve(request, *args, **kwargs)
