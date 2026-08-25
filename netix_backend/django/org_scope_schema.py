"""OpenAPI advertising for ``?organization=<id>``: the three spectacular mechanisms plus the raw dict.

Deliberately separate from :mod:`netix_backend.django.org_scope`, and every drf-spectacular import is
function-local, so the runtime mixin stays importable without the ``spectacular`` extra.
``description`` is required with no default on all four helpers: the library must never be able to
silently rewrite an adopter's published prose.
"""

from __future__ import annotations

from typing import Any

from netix_backend.django.org_scope import ORGANIZATION_QUERY_PARAM

__all__ = [
    "SUPERUSER_ORG_SCOPE_ACTIONS",
    "superuser_org_scope_autoschema",
    "superuser_org_scope_parameter",
    "superuser_org_scope_parameter_dict",
    "superuser_org_scope_schema",
]

SUPERUSER_ORG_SCOPE_ACTIONS: tuple[str, ...] = ("list", "retrieve", "create", "update", "partial_update", "destroy")


def superuser_org_scope_parameter(*, description: str, name: str = ORGANIZATION_QUERY_PARAM) -> Any:
    """The bare ``OpenApiParameter``, for a viewset that assembles its own ``extend_schema``."""
    from drf_spectacular.utils import OpenApiParameter

    return OpenApiParameter(
        name=name,
        type=int,
        location=OpenApiParameter.QUERY,
        required=False,
        description=description,
    )


def superuser_org_scope_schema(*actions: str, description: str, name: str = ORGANIZATION_QUERY_PARAM) -> Any:
    """Class decorator advertising the parameter on ``actions``, defaulting to the six CRUD actions."""
    from drf_spectacular.utils import extend_schema, extend_schema_view

    decorated = extend_schema(parameters=[superuser_org_scope_parameter(description=description, name=name)])
    return extend_schema_view(**dict.fromkeys(actions or SUPERUSER_ORG_SCOPE_ACTIONS, decorated))


def superuser_org_scope_autoschema(*, description: str, base: Any = None, name: str = ORGANIZATION_QUERY_PARAM) -> Any:
    """An ``AutoSchema`` subclass appending the parameter to every operation of the views that bind it."""
    from drf_spectacular.openapi import AutoSchema

    parent: Any = AutoSchema if base is None else base

    class SuperuserOrgScopeAutoSchema(parent):
        def get_override_parameters(self) -> list[Any]:
            return [
                *super().get_override_parameters(),
                superuser_org_scope_parameter(description=description, name=name),
            ]

    return SuperuserOrgScopeAutoSchema


def superuser_org_scope_parameter_dict(*, description: str, name: str = ORGANIZATION_QUERY_PARAM) -> dict[str, Any]:
    """The raw OpenAPI parameter dict for DRF-native ``AutoSchema.get_operation`` injection; imports nothing.

    The key order is the one ``generateschema`` emits, so a repo appending this to ``operation["parameters"]``
    produces a byte-identical contract.
    """
    return {
        "name": name,
        "required": False,
        "in": "query",
        "description": description,
        "schema": {"type": "integer"},
    }
