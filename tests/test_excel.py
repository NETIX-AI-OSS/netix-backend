"""The shared xlsx export viewset: real spreadsheet bytes, the binary schema, and the style presets."""

import json
import subprocess
import sys
from typing import Any

import pytest
from rest_framework.test import APIClient

from netix_backend.django import excel as excel_module
from netix_backend.django.excel import (
    EXCEL_STYLE_APTOS,
    EXCEL_STYLE_ARIAL,
    WRAP_ALIGNMENT,
    BaseExcelViewSet,
    DocumentedExcelViewSet,
    ExcelExportMixin,
    ExcelExportViewSet,
    ScopedExcelViewSet,
    excel_style,
)
from netix_backend.django.excel_aio import AsyncExcelViewSet
from netix_backend.django.pagination import BigLimitOffsetPagination
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


class TestCoreLayering:
    def test_the_v1_name_still_resolves_to_the_scoped_variant(self) -> None:
        assert BaseExcelViewSet is ScopedExcelViewSet

    def test_the_scoped_variant_is_the_documented_core_plus_envoy(self) -> None:
        from netix_backend.django.views import EnvoyScopedQuerysetMixin

        assert issubclass(ScopedExcelViewSet, DocumentedExcelViewSet)
        assert issubclass(ScopedExcelViewSet, EnvoyScopedQuerysetMixin)

    def test_the_core_carries_no_envoy_scoping(self) -> None:
        from netix_backend.django.views import EnvoyScopedQuerysetMixin

        # user-management's scoping branches on request.apikey/request.user, so it can only ever adopt this.
        assert not issubclass(ExcelExportViewSet, EnvoyScopedQuerysetMixin)

    def test_the_core_leaves_the_openapi_contract_alone(self) -> None:
        # tag-service publishes no xlsx schema today; adopting the core must not add one.
        assert "schema" not in ExcelExportViewSet.__dict__
        assert "schema" not in ExcelExportMixin.__dict__

    def test_the_style_and_renderer_attributes_are_shared_with_the_async_shape(self) -> None:
        assert issubclass(AsyncExcelViewSet, ExcelExportMixin)
        assert AsyncExcelViewSet.filename == ExcelExportViewSet.filename
        assert AsyncExcelViewSet.column_header == ExcelExportViewSet.column_header

    def test_the_export_paginator_is_pinnable_per_repo(self) -> None:
        # The fleet's four export page sizes all differ; the default is documented, not prescriptive.
        assert ExcelExportViewSet.pagination_class is BigLimitOffsetPagination

    def test_an_unknown_attribute_still_raises(self) -> None:
        with pytest.raises(AttributeError, match="has no attribute 'Nope'"):
            excel_module.Nope  # noqa: B018

    def test_importing_the_core_does_not_pull_the_envoy_chain(self) -> None:
        source = (
            "import sys; import netix_backend.django.excel as excel; "
            "assert 'envoy_pyauth' not in sys.modules, sorted(sys.modules); "
            "print(excel.ExcelExportViewSet.filename)"
        )
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "export.xlsx"


class TestUnscopedCoreEndpoint:
    def test_the_core_exports_every_row(self, client: APIClient, widget: ScopedWidget) -> None:
        ScopedWidget.objects.create(label="theirs", organization_id=99)
        response = client.get("/api/excel-unscoped/", **ENVOY)
        assert response.status_code == 200
        assert response.content[:2] == b"PK"

    def test_the_core_declares_no_binary_response_override(self) -> None:
        assert not hasattr(ExcelExportViewSet.list, "kwargs")


class TestAsyncExcelViewSet:
    def test_the_async_list_renders_a_spreadsheet(self, client: APIClient, widget: ScopedWidget) -> None:
        response = client.get("/api/async-excel/", **ENVOY)
        assert response.status_code == 200
        assert response.content[:2] == b"PK"
        assert response["content-disposition"] == "attachment; filename=export.xlsx"

    def test_the_async_retrieve_renders_a_spreadsheet(self, client: APIClient, widget: ScopedWidget) -> None:
        response = client.get(f"/api/async-excel/{widget.pk}/", **ENVOY)
        assert response.status_code == 200
        assert response.content[:2] == b"PK"

    def test_the_sync_list_delegates_to_the_async_one(self) -> None:
        # user-management keeps a sync `list` so a plain router entry still resolves.
        view = AsyncExcelViewSet()
        recorded: list[str] = []

        async def alist(request: Any, *args: Any, **kwargs: Any) -> str:
            recorded.append("alist")
            return "spreadsheet"

        view.alist = alist  # type: ignore[method-assign, assignment]
        assert view.list(None) == "spreadsheet"  # type: ignore[arg-type]
        assert recorded == ["alist"]

    def test_the_binary_response_is_declared_on_all_three_handlers(self) -> None:
        assert isinstance(AsyncExcelViewSet.schema, XlsxExportAutoSchema)
        for handler in (AsyncExcelViewSet.list, AsyncExcelViewSet.alist, AsyncExcelViewSet.aretrieve):
            assert _declared_responses(handler)[200] is XLSX_BINARY_RESPONSE
