"""OpenAPI advertising for ``?include_history=true``; needs the ``spectacular`` extra.

Split from :mod:`netix_backend.django.change_history` for the same reason
:mod:`netix_backend.django.org_scope_schema` is split from ``org_scope``: the model mixin and the
actor middleware must stay importable in a repo that does not ship drf-spectacular.

``HISTORY_PARAMETER`` is the ready-made parameter; :func:`history_parameter` is the same thing with
the prose (and the parameter name) under the adopter's control.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter

from netix_backend.django.change_history import HISTORY_QUERY_PARAM

__all__ = ["HISTORY_DESCRIPTION", "HISTORY_PARAMETER", "history_parameter"]

HISTORY_DESCRIPTION = (
    "Include the record's change_history (who changed what, when). Omitted from the "
    "response by default to keep list payloads small."
)


def history_parameter(*, description: str = HISTORY_DESCRIPTION, name: str = HISTORY_QUERY_PARAM) -> Any:
    """The boolean query parameter, for a viewset that assembles its own ``extend_schema``."""
    return OpenApiParameter(
        name=name,
        type=OpenApiTypes.BOOL,
        location=OpenApiParameter.QUERY,
        required=False,
        description=description,
    )


# Module-level so `extend_schema(parameters=[HISTORY_PARAMETER])` reads the same as it does in the
# repos this was lifted from; OpenApiParameter is inert, nothing mutates it.
HISTORY_PARAMETER = history_parameter()
