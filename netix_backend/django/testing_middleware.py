"""Test-only Envoy identity middleware: the seven repo-local copies as two settings-driven classes."""

from __future__ import annotations

from typing import Any, ClassVar, cast
from unittest.mock import patch

from django.conf import settings
from django.http import HttpResponseBase
from django.utils.deprecation import MiddlewareMixin
from envoy_pyauth.middleware import AuthorizationMiddleware

from netix_backend.django.testing import DEFAULT_ENVOY_IDENTITY, DEFAULT_TEST_BEARER

__all__ = [
    "EnvoyTestAuthorizationMiddleware",
    "ExplicitEnvoyIdentityMiddleware",
    "configured_bearer",
    "configured_header_guard",
    "configured_identity",
]


def configured_identity() -> dict[str, Any]:
    """``NETIX_TEST_ENVOY_IDENTITY`` or the minimal default, as an isolated copy per request."""
    identity: Any = getattr(settings, "NETIX_TEST_ENVOY_IDENTITY", None) or DEFAULT_ENVOY_IDENTITY
    return {**identity, "permissions": list(identity.get("permissions", ()))}


def configured_bearer() -> str:
    """The token injected when a request carries no Authorization header of its own."""
    return str(getattr(settings, "NETIX_TEST_ENVOY_BEARER", DEFAULT_TEST_BEARER))


def configured_header_guard() -> bool:
    """``NETIX_TEST_ENVOY_HEADER_GUARD``: the settings form of the subclass update-service and stormbreaker ship."""
    return bool(getattr(settings, "NETIX_TEST_ENVOY_HEADER_GUARD", True))


# Resolves a test-only bearer at envoy-pyauth v2's resolver seam; production keeps failing closed.
class EnvoyTestAuthorizationMiddleware(AuthorizationMiddleware):
    # A request that brings its own credential is resolved for real, so permission-denial tests stay honest.
    # Either the class attribute or NETIX_TEST_ENVOY_HEADER_GUARD=False turns the guard off.
    header_guard: ClassVar[bool] = True

    def process_view(
        self,
        request: Any,
        view_func: Any,
        *view_args: Any,
        **view_kwargs: Any,
    ) -> HttpResponseBase | None:
        # envoy_pyauth ships no py.typed marker, so its return type arrives as Any.
        if self.header_guard and configured_header_guard() and request.META.get("HTTP_AUTHORIZATION"):
            return cast("HttpResponseBase | None", super().process_view(request, view_func, *view_args, **view_kwargs))
        request.META["HTTP_AUTHORIZATION"] = configured_bearer()
        with patch("envoy_pyauth.middleware._resolve", return_value=configured_identity()):
            return cast("HttpResponseBase | None", super().process_view(request, view_func, *view_args, **view_kwargs))


# Assigns request.envoy directly, for suites that do not want to exercise the resolver seam at all.
class ExplicitEnvoyIdentityMiddleware(MiddlewareMixin):
    def process_view(self, request: Any, _view_func: Any, *_view_args: Any, **_view_kwargs: Any) -> None:
        request.envoy = configured_identity()
