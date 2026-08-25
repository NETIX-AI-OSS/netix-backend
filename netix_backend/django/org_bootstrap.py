"""The internal org-bootstrap endpoint six services ship: POST clones the org-0 catalog, DELETE tears it down."""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Protocol

from envoy_pyauth.decorator import envoy_internal_only
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

__all__ = (
    "CloneError",
    "ClonePrimitives",
    "OrgBootstrapBaseView",
    "OrgBootstrapSerializer",
    "OrgTeardownSerializer",
    "TeardownPrimitives",
    "build_org_bootstrap_view",
)

logger = logging.getLogger(__name__)


class CloneError(RuntimeError):
    """A clone or teardown could not be completed; the whole run is rolled back."""


class ClonePrimitives(Protocol):
    """The clone half of the driver contract: one positional target org, the rest keyword."""

    def __call__(self, target_org: int, source_org: int = ..., run_id: str | None = ...) -> dict[str, int]: ...


class TeardownPrimitives(Protocol):
    """The teardown half; ``run_id`` is accepted on the wire but never reaches this call."""

    def __call__(self, target_org: int) -> dict[str, int]: ...


# Serializers stay module-level so a subclass can reuse them for its own actions.
class OrgBootstrapSerializer(serializers.Serializer[Any]):
    organization_id = serializers.IntegerField(min_value=1)
    source_organization_id = serializers.IntegerField(required=False, default=0)
    run_id = serializers.CharField(required=False, allow_blank=True, default="")


class OrgTeardownSerializer(serializers.Serializer[Any]):
    organization_id = serializers.IntegerField(min_value=1)
    # Kept for wire compatibility: all six accept it and none forwards it to the teardown driver.
    run_id = serializers.CharField(required=False, allow_blank=True, default="")


# Internal-only in the fleet's sense: @envoy_internal_only() admits a platform-internal identity and nothing else.
# Since envoy-pyauth v2.0.0 an absent identity is a 401 rather than an implicit internal call, so neither an
# anonymous caller nor a tenant identity can bootstrap (or tear down) another organization.
# The guard is the decorator, not a permission class; DRF's defaults would 403 first, hence the empty tuples.
class OrgBootstrapBaseView(APIView):
    schema = None
    permission_classes: Any = ()
    authentication_classes: Any = ()

    # Bound by build_org_bootstrap_view(); staticmethod, or attribute access would bind them as methods.
    clone_primitives: ClassVar[Any] = None
    teardown_primitives: ClassVar[Any] = None
    clone_error: ClassVar[type[Exception]] = CloneError
    catalog: ClassVar[str] = ""

    @envoy_internal_only()
    def post(self, request: Request) -> Response:
        serializer = OrgBootstrapSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            stats = self.clone_primitives(
                data["organization_id"],
                source_org=data["source_organization_id"],
                run_id=data["run_id"] or None,
            )
        except self.clone_error as exc:
            # A blocked clone (an inverted-ownership edge, say) is a 409, not a 500: the caller records it
            # and an operator re-drives after fixing the cause.
            self._log_refusal(data["organization_id"], exc)
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"organization_id": data["organization_id"], "stats": stats}, status=status.HTTP_200_OK)

    @envoy_internal_only()
    def delete(self, request: Request) -> Response:
        serializer = OrgTeardownSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        organization_id = serializer.validated_data["organization_id"]
        try:
            stats = self.teardown_primitives(organization_id)
        except self.clone_error as exc:
            self._log_refusal(organization_id, exc)
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response({"organization_id": organization_id, "stats": stats}, status=status.HTTP_200_OK)

    def _log_refusal(self, organization_id: int, exc: Exception) -> None:
        # DELETE logs too; the six copies swallowed the teardown message, which left operators with only a 409.
        if self.catalog:
            logger.warning("org-bootstrap refused for org %s (%s): %s", organization_id, self.catalog, exc)
            return
        logger.warning("org-bootstrap refused for org %s: %s", organization_id, exc)


def build_org_bootstrap_view(
    *,
    clone: ClonePrimitives,
    teardown: TeardownPrimitives,
    error: type[Exception] = CloneError,
    name: str = "OrgBootstrapView",
    catalog: str = "",
    base: type[OrgBootstrapBaseView] = OrgBootstrapBaseView,
) -> type[OrgBootstrapBaseView]:
    """Bind one service's clone/teardown callables and its own ``CloneError`` into the shared view."""
    attributes: dict[str, Any] = {
        "clone_primitives": staticmethod(clone),
        "teardown_primitives": staticmethod(teardown),
        "clone_error": error,
        "catalog": catalog,
    }
    return type(name, (base,), attributes)
