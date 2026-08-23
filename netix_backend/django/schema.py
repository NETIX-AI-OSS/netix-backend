"""drf-spectacular schema classes shared by the async (adrf) and xlsx-export viewsets."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from drf_spectacular.openapi import AutoSchema
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse

# Overrides the XLSX response so drf-spectacular doesn't advertise it as JSON.
XLSX_BINARY_RESPONSE = OpenApiResponse(
    response=OpenApiTypes.BINARY,
    description="A binary .xlsx spreadsheet download.",
)


class AsyncActionAutoSchema(AutoSchema):
    """AutoSchema that understands adrf's async action names."""

    ASYNC_ACTION_MAP: ClassVar[dict[str, str]] = {
        "alist": "list",
        "aretrieve": "retrieve",
        "acreate": "create",
        "aupdate": "update",
        "apartial_update": "partial_update",
        "adestroy": "destroy",
    }

    _LIST_ACTION_NAMES: ClassVar[set[str]] = {"list", "alist"}
    _RETRIEVE_ACTION_NAMES: ClassVar[set[str]] = {"retrieve", "aretrieve"}

    def _get_action(self) -> str | None:
        """Get the current action name from the view."""
        action = getattr(self.view, "action", None)
        if isinstance(action, str):
            return action
        action_map = getattr(self.view, "action_map", None)
        if isinstance(action_map, Mapping):
            mapped_action = action_map.get(self.method.lower())
            if isinstance(mapped_action, str):
                return mapped_action
        return None

    def _get_normalized_action(self) -> str | None:
        """Get action name normalized to sync equivalent."""
        action = self._get_action()
        if action is None:
            return None
        return self.ASYNC_ACTION_MAP.get(action, action)

    def _is_list_view(self, serializer: Any = None) -> bool:
        action = self._get_action()
        if action in self._LIST_ACTION_NAMES:
            return True
        return bool(super()._is_list_view(serializer))

    def get_operation_id(self) -> str:
        """Override to use normalized action name for operation ID generation."""
        original_action = getattr(self.view, "action", None)
        normalized = self._get_normalized_action()

        if normalized and original_action != normalized:
            # `action` is on ViewSets, not the APIView type drf-stubs assigns.
            setattr(self.view, "action", normalized)  # noqa: B010
            try:
                return str(super().get_operation_id())
            finally:
                setattr(self.view, "action", original_action)  # noqa: B010

        return str(super().get_operation_id())

    def get_override_parameters(self) -> Any:
        """Override to resolve @extend_schema decorators from async method names."""
        params = super().get_override_parameters()
        if params:
            return params

        action = self._get_action()
        if action is None:
            return params

        async_action = f"a{action}" if not action.startswith("a") else None
        sync_action = self.ASYNC_ACTION_MAP.get(action)

        for alt_action in [async_action, sync_action]:
            if alt_action:
                method = getattr(self.view, alt_action, None)
                if method and hasattr(method, "kwargs"):
                    schema_override = method.kwargs.get("schema")
                    parameters = self._extract_extend_schema_parameters(schema_override)
                    if parameters:
                        return list(parameters)

        return params

    @staticmethod
    def _extract_extend_schema_parameters(schema_override: Any) -> Any:
        """Extract parameter overrides from an extend_schema override."""
        if not schema_override:
            return None

        if isinstance(schema_override, Mapping):
            parameters = schema_override.get("parameters")
            if parameters:
                return parameters

        if inspect.isclass(schema_override):
            override_fn = getattr(schema_override, "get_override_parameters", None)
            if override_fn and getattr(override_fn, "__closure__", None):
                try:
                    for cell in override_fn.__closure__:
                        cell_value = cell.cell_contents
                        if isinstance(cell_value, Sequence) and cell_value:
                            return cell_value
                except (AttributeError, TypeError, ValueError):
                    # Defensive: closure structure may change across drf-spectacular versions.
                    return None

        return None

    def get_description(self) -> str:  # type: ignore[override]
        """Override to look at both sync and async method docstrings."""
        description = super().get_description()
        if description:
            return str(description)

        action = self._get_action()
        if action:
            async_action = f"a{action}" if not action.startswith("a") else None
            sync_action = self.ASYNC_ACTION_MAP.get(action)

            for alt_action in [async_action, sync_action]:
                if alt_action:
                    method = getattr(self.view, alt_action, None)
                    if method and method.__doc__:
                        return str(method.__doc__)

        return ""


class XlsxExportAutoSchema(AsyncActionAutoSchema):
    """AutoSchema for XLSXRenderer-backed export viewsets."""

    def _is_list_view(self, serializer: Any = None) -> bool:
        if serializer is not None:
            return False
        return bool(super()._is_list_view())
