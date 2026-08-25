"""Tests for the shared drf-spectacular schema classes."""

from __future__ import annotations

import types
from typing import Any

import pytest
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers
from rest_framework.generics import GenericAPIView

from netix_backend.django.schema import (
    XLSX_BINARY_RESPONSE,
    AsyncActionAutoSchema,
    XlsxExportAutoSchema,
)


class WidgetSerializer(serializers.Serializer):
    name = serializers.CharField()


class WidgetView(GenericAPIView):
    serializer_class = WidgetSerializer

    def alist(self) -> None:
        """List every widget."""

    def list(self) -> None:
        pass

    def aexport(self) -> None:
        pass

    def export(self) -> None:
        pass


# Deliberately docstring-free: spectacular falls back to the view docstring for descriptions.
class FakeView:
    pass


def make_view(**attrs: Any) -> WidgetView:
    view = WidgetView()
    # APIRequestFactory is not involved, so the attributes spectacular reads must be supplied.
    view.request = None  # type: ignore[assignment]
    view.format_kwarg = None
    for name, value in attrs.items():
        setattr(view, name, value)
    return view


def make_schema(
    view: Any,
    schema_class: type[AsyncActionAutoSchema] = AsyncActionAutoSchema,
    method: str = "GET",
    path: str = "/api/v1/widgets/",
) -> AsyncActionAutoSchema:
    schema = schema_class()
    schema.view = view
    schema.method = method
    schema.path = path
    schema.path_prefix = "/api/v1"
    schema.path_regex = path
    return schema


class TestXlsxBinaryResponse:
    def test_is_a_binary_response(self) -> None:
        assert XLSX_BINARY_RESPONSE.response is OpenApiTypes.BINARY
        assert XLSX_BINARY_RESPONSE.description == "A binary .xlsx spreadsheet download."


class TestGetAction:
    def test_string_action_attribute(self) -> None:
        assert make_schema(make_view(action="alist"))._get_action() == "alist"

    def test_falls_back_to_the_action_map(self) -> None:
        assert make_schema(make_view(action_map={"get": "alist"}))._get_action() == "alist"

    def test_action_map_without_the_current_method(self) -> None:
        assert make_schema(make_view(action_map={"post": "acreate"}))._get_action() is None

    def test_non_string_mapped_action(self) -> None:
        assert make_schema(make_view(action_map={"get": 5}))._get_action() is None

    def test_action_map_that_is_not_a_mapping(self) -> None:
        assert make_schema(make_view(action_map="nonsense"))._get_action() is None

    def test_neither_action_nor_action_map(self) -> None:
        assert make_schema(FakeView())._get_action() is None


class TestGetNormalizedAction:
    def test_async_action_is_mapped_to_its_sync_equivalent(self) -> None:
        assert make_schema(make_view(action="apartial_update"))._get_normalized_action() == "partial_update"

    def test_unknown_action_is_returned_unchanged(self) -> None:
        assert make_schema(make_view(action="export"))._get_normalized_action() == "export"

    def test_no_action(self) -> None:
        assert make_schema(FakeView())._get_normalized_action() is None


class TestIsListView:
    def test_async_list_action(self) -> None:
        assert make_schema(make_view(action="alist"))._is_list_view() is True

    def test_sync_list_action(self) -> None:
        assert make_schema(make_view(action="list"))._is_list_view() is True

    def test_retrieve_action_delegates_to_spectacular(self) -> None:
        assert make_schema(make_view(action="retrieve"))._is_list_view() is False


class TestGetOperationId:
    def test_async_action_uses_the_sync_operation_id(self) -> None:
        view = make_view(action="alist")
        schema = make_schema(view)
        assert schema.get_operation_id() == "widgets_list"
        assert view.action == "alist"

    def test_action_needing_no_normalisation(self) -> None:
        assert make_schema(make_view(action="retrieve")).get_operation_id() == "widgets_retrieve"

    def test_view_without_an_action(self) -> None:
        assert make_schema(FakeView(), method="POST").get_operation_id() == "widgets_create"


class TestGetOverrideParameters:
    def test_spectacular_parameters_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(AutoSchema, "get_override_parameters", lambda self: ["from-super"])
        assert make_schema(make_view(action="list")).get_override_parameters() == ["from-super"]

    def test_no_action_returns_the_default(self) -> None:
        assert make_schema(FakeView()).get_override_parameters() == []

    def test_resolves_the_async_variant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WidgetView.alist, "kwargs", {"schema": {"parameters": ["q"]}}, raising=False)
        assert make_schema(make_view(action="list")).get_override_parameters() == ["q"]

    def test_resolves_the_sync_variant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WidgetView.list, "kwargs", {"schema": {"parameters": ["q"]}}, raising=False)
        assert make_schema(make_view(action="alist")).get_override_parameters() == ["q"]

    def test_methods_without_extend_schema_kwargs(self) -> None:
        assert make_schema(make_view(action="list")).get_override_parameters() == []

    def test_empty_parameters_keep_looking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(WidgetView.alist, "kwargs", {"schema": {"parameters": []}}, raising=False)
        assert make_schema(make_view(action="list")).get_override_parameters() == []

    def test_missing_alternate_methods(self) -> None:
        assert make_schema(make_view(action="ghost")).get_override_parameters() == []


def make_closure_holder(*cells: types.CellType) -> type:
    """A stand-in for an extend_schema-generated class whose override closes over parameters."""

    class _Fn:
        __closure__ = cells

    class _SchemaOverride:
        get_override_parameters = _Fn()

    return _SchemaOverride


class TestExtractExtendSchemaParameters:
    extract = staticmethod(AsyncActionAutoSchema._extract_extend_schema_parameters)

    def test_no_override(self) -> None:
        assert self.extract(None) is None

    def test_mapping_with_parameters(self) -> None:
        assert self.extract({"parameters": ["q"]}) == ["q"]

    def test_mapping_without_parameters(self) -> None:
        assert self.extract({"other": 1}) is None

    def test_class_closure_holding_a_sequence(self) -> None:
        assert self.extract(make_closure_holder(types.CellType(["q"]))) == ["q"]

    def test_class_closure_holding_a_non_sequence(self) -> None:
        assert self.extract(make_closure_holder(types.CellType(3))) is None

    def test_class_closure_holding_an_empty_cell(self) -> None:
        assert self.extract(make_closure_holder(types.CellType())) is None

    def test_class_without_an_override(self) -> None:
        class _SchemaOverride:
            pass

        assert self.extract(_SchemaOverride) is None

    def test_class_whose_override_has_no_closure(self) -> None:
        class _SchemaOverride:
            def get_override_parameters(self) -> list[str]:
                return []

        assert self.extract(_SchemaOverride) is None


class TestGetDescription:
    def test_spectacular_description_wins(self) -> None:
        assert make_schema(make_view(action="alist")).get_description() == "List every widget."

    def test_falls_back_to_the_async_docstring(self) -> None:
        assert make_schema(make_view(action="list")).get_description() == "List every widget."

    def test_no_action(self) -> None:
        assert make_schema(FakeView()).get_description() == ""

    def test_alternate_methods_without_docstrings(self) -> None:
        assert make_schema(make_view(action="export")).get_description() == ""

    def test_async_action_with_no_sync_equivalent(self) -> None:
        assert make_schema(make_view(action="aexport")).get_description() == ""

    def test_missing_alternate_methods(self) -> None:
        assert make_schema(make_view(action="ghost")).get_description() == ""


class TestXlsxExportAutoSchema:
    def test_a_supplied_serializer_is_never_a_list_view(self) -> None:
        schema = make_schema(make_view(action="alist"), XlsxExportAutoSchema)
        assert schema._is_list_view(WidgetSerializer()) is False

    def test_without_a_serializer_it_behaves_like_the_parent(self) -> None:
        schema = make_schema(make_view(action="alist"), XlsxExportAutoSchema)
        assert schema._is_list_view() is True

    def test_inherits_the_async_action_handling(self) -> None:
        assert issubclass(XlsxExportAutoSchema, AsyncActionAutoSchema)


def test_lib_class_docstrings_do_not_leak_into_operation_descriptions() -> None:
    # The pilot adoption hit this: a docstring anywhere in the view MRO becomes every endpoint's description.
    import json

    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator(urlconf="tests.urls").get_schema(request=None, public=True)
    text = json.dumps(schema)
    for leaked in (
        "Scoped reads, pinned writes",
        "adrf-routed soft delete",
        "Read-only xlsx export",
        "Atomicity as a knob",
        "keyword-only signature",
        "the smallest adoptable base",
        "adrf routes GET",
        "Internal-only in the fleet's sense",
    ):
        assert leaked not in text


def test_no_view_mro_class_carries_a_docstring() -> None:
    # Order-proof form of the leak test: a docstring on ANY of these classes reaches consumers' schemas.
    from netix_backend.django import excel, excel_aio, excel_envoy, org_bootstrap, org_scope, views, views_aio

    for cls in (
        org_scope.SuperuserOrgScopeMixin,
        views.BaseViewSet,
        views.EnvoyPermissionMixin,
        views.EnvoyScopedQuerysetMixin,
        views.TenantWriteScopeMixin,
        views.AtomicWriteMixin,
        views.SoftDeleteMixin,
        views.ListErrorMappingMixin,
        views.CreateListModelMixin,
        views.UpdateListModelMixin,
        views.IncludeDeletedMixin,
        views_aio.AsyncSoftDeleteMixin,
        views_aio.AsyncBaseViewSet,
        excel.ExcelExportMixin,
        excel.ExcelExportViewSet,
        excel.DocumentedExcelViewSet,
        excel_envoy.ScopedExcelViewSet,
        excel.BaseExcelViewSet,
        excel_aio.AsyncExcelViewSet,
        org_bootstrap.OrgBootstrapBaseView,
        org_bootstrap.build_org_bootstrap_view(clone=lambda target_org, **_: {}, teardown=lambda target_org: {}),
    ):
        assert cls.__dict__.get("__doc__") is None, f"{cls.__name__} docstring leaks into OpenAPI descriptions"
