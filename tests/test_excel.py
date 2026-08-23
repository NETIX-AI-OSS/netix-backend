"""The shared xlsx export viewset: real spreadsheet bytes, the binary schema, and the style presets."""

import json
from typing import Any

import pytest
from rest_framework.test import APIClient

from netix_backend.django.excel import (
    EXCEL_STYLE_APTOS,
    EXCEL_STYLE_ARIAL,
    WRAP_ALIGNMENT,
    BaseExcelViewSet,
    excel_style,
)
from netix_backend.django.schema import XLSX_BINARY_RESPONSE, XlsxExportAutoSchema
from tests.models import ScopedWidget

TENANT = 7
ENVOY = {"HTTP_X_TEST_ENVOY": json.dumps({"organization": TENANT, "permissions": []})}


@pytest.fixture
def client() -> APIClient:
    return APIClient()


@pytest.fixture
def widget(db: Any) -> ScopedWidget:
    return ScopedWidget.objects.create(label="exported", organization_id=TENANT)


def test_list_renders_a_real_spreadsheet(client: APIClient, widget: ScopedWidget) -> None:
    response = client.get("/api/excel/", **ENVOY)
    assert response.status_code == 200
    # An xlsx file is a zip archive, so the magic bytes are the honest assertion.
    assert response.content[:2] == b"PK"
    assert response["content-disposition"] == "attachment; filename=export.xlsx"


def test_retrieve_renders_a_spreadsheet_too(client: APIClient, widget: ScopedWidget) -> None:
    response = client.get(f"/api/excel/{widget.pk}/", **ENVOY)
    assert response.status_code == 200
    assert response.content[:2] == b"PK"


def test_exports_are_organization_scoped(client: APIClient, widget: ScopedWidget) -> None:
    other = ScopedWidget.objects.create(label="theirs", organization_id=99)
    response = client.get(f"/api/excel/{other.pk}/", **ENVOY)
    assert response.status_code == 404


def _declared_responses(handler: Any) -> dict[Any, Any]:
    """The ``responses=`` extend_schema passed, which it keeps in the generated schema class's closure."""
    schema_class = handler.kwargs["schema"]
    cells = [cell.cell_contents for cell in schema_class.get_response_serializers.__closure__]
    return next(cell for cell in cells if isinstance(cell, dict))


def test_the_binary_response_schema_is_unconditional() -> None:
    assert isinstance(BaseExcelViewSet.schema, XlsxExportAutoSchema)
    assert _declared_responses(BaseExcelViewSet.list)[200] is XLSX_BINARY_RESPONSE
    assert _declared_responses(BaseExcelViewSet.retrieve)[200] is XLSX_BINARY_RESPONSE


def test_the_two_style_presets_differ_only_in_their_fonts() -> None:
    assert EXCEL_STYLE_ARIAL["column_header"]["style"]["font"]["name"] == "Arial"
    assert EXCEL_STYLE_ARIAL["body"]["style"]["font"]["size"] == 14
    assert EXCEL_STYLE_APTOS["column_header"]["style"]["font"]["name"] == "Aptos Narrow"
    assert EXCEL_STYLE_APTOS["body"]["style"]["font"]["size"] == 11
    assert EXCEL_STYLE_ARIAL["body"]["style"]["border_side"] == EXCEL_STYLE_APTOS["body"]["style"]["border_side"]


def test_wrap_adds_the_alignment_block_without_touching_the_preset() -> None:
    wrapped = excel_style(EXCEL_STYLE_APTOS, wrap=True)
    plain = excel_style(EXCEL_STYLE_APTOS)
    assert wrapped["body"]["style"]["alignment"] == WRAP_ALIGNMENT
    assert "alignment" not in plain["body"]["style"]
    assert "alignment" not in EXCEL_STYLE_APTOS["body"]["style"]


def test_the_default_viewset_style_is_a_private_copy() -> None:
    assert BaseExcelViewSet.column_header == EXCEL_STYLE_ARIAL["column_header"]
    assert BaseExcelViewSet.column_header is not EXCEL_STYLE_ARIAL["column_header"]
