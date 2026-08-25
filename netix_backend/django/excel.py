"""Envoy-free xlsx export core: the two style presets, the renderer wiring and the binary response schema."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Final

from drf_excel.mixins import XLSXFileMixin
from drf_excel.renderers import XLSXRenderer
from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet

from netix_backend.django.pagination import BigLimitOffsetPagination
from netix_backend.django.schema import XLSX_BINARY_RESPONSE, XlsxExportAutoSchema

if TYPE_CHECKING:
    # Declared for type checkers and linters only; at runtime __getattr__ below resolves them on demand.
    from netix_backend.django.excel_envoy import BaseExcelViewSet, ScopedExcelViewSet

__all__ = [
    "EXCEL_STYLE_APTOS",
    "EXCEL_STYLE_ARIAL",
    "WRAP_ALIGNMENT",
    "BaseExcelViewSet",
    "DocumentedExcelViewSet",
    "ExcelExportMixin",
    "ExcelExportViewSet",
    "ScopedExcelViewSet",
    "excel_style",
]

_BORDER_SIDE: Final[dict[str, Any]] = {"border_style": "thin", "color": "FF000000"}

WRAP_ALIGNMENT: Final[dict[str, Any]] = {
    "horizontal": "left",
    "vertical": "center",
    "wrapText": True,
    "shrink_to_fit": True,
}

# The scoped variants live in excel_envoy so importing this module never pulls the envoy import chain.
_ENVOY_EXPORTS: Final[frozenset[str]] = frozenset({"BaseExcelViewSet", "ScopedExcelViewSet"})


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


# The renderer, filename and style attributes, on no viewset base, so the sync and adrf shapes share them.
class ExcelExportMixin:
    renderer_classes: Any = (XLSXRenderer,)
    filename = "export.xlsx"
    # Pin your repo's own numbers with limit_offset_pagination(); the fleet's export page sizes all differ.
    pagination_class: Any = BigLimitOffsetPagination
    # filter_backends stays unset so DRF's DEFAULT_FILTER_BACKENDS still apply; declare your own list per repo.
    column_header: ClassVar[dict[str, Any]] = _DEFAULT_STYLE["column_header"]
    body: ClassVar[dict[str, Any]] = _DEFAULT_STYLE["body"]


# Read-only xlsx export with no organization scoping and no OpenAPI opinion; the smallest adoptable base.
class ExcelExportViewSet(ExcelExportMixin, XLSXFileMixin, ReadOnlyModelViewSet[Any]):
    pass


# Adds the binary response schema, so a service that publishes a contract cannot advertise the export as JSON.
class DocumentedExcelViewSet(ExcelExportViewSet):
    schema = XlsxExportAutoSchema()

    @extend_schema(responses={200: XLSX_BINARY_RESPONSE})
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().list(request, *args, **kwargs)

    @extend_schema(responses={200: XLSX_BINARY_RESPONSE})
    def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return super().retrieve(request, *args, **kwargs)


def __getattr__(name: str) -> Any:
    # Compatibility shim: BaseExcelViewSet stays importable from here without this module importing envoy.
    if name in _ENVOY_EXPORTS:
        from netix_backend.django import excel_envoy

        value = getattr(excel_envoy, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
