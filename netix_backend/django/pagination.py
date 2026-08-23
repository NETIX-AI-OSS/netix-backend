"""Shared limit/offset pagination with request-time, settings-driven limits."""

from __future__ import annotations

from typing import Any, ClassVar, cast

from django.conf import settings
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.request import Request


class NetixLimitOffsetPagination(LimitOffsetPagination):
    """Limit/offset paginator whose limits are read from settings on every request."""

    # `limit`/`offset` are a cross-service contract: report-engine walks other services' APIs with them.
    limit_query_param = "limit"
    offset_query_param = "offset"
    default_limit = 1000
    max_limit: int | None = 1000
    setting_prefix: ClassVar[str | None] = None

    def _setting(self, suffix: str, fallback: int | None) -> int | None:
        prefix = self.setting_prefix
        if prefix is None:
            return fallback
        value = getattr(settings, f"{prefix}_{suffix}", fallback)
        return None if value is None else int(value)

    def get_default_limit(self) -> int | None:
        """The page size used when the request carries no `limit`."""
        return self._setting("DEFAULT_LIMIT", type(self).default_limit)

    def get_max_limit(self) -> int | None:
        """The ceiling applied to a caller-supplied `limit`."""
        return self._setting("MAX_LIMIT", type(self).max_limit)

    def get_limit(self, request: Request) -> int | None:
        # Bind both limits per request so override_settings and env reloads take effect.
        self.default_limit = cast("int", self.get_default_limit())
        self.max_limit = self.get_max_limit()
        return super().get_limit(request)


def limit_offset_pagination(
    *,
    default_limit: int,
    max_limit: int | None,
    name: str | None = None,
    setting_prefix: str | None = None,
) -> type[NetixLimitOffsetPagination]:
    """Build a NetixLimitOffsetPagination subclass so a repo pins its numbers in one line."""
    attrs: dict[str, Any] = {
        "default_limit": default_limit,
        "max_limit": max_limit,
        "setting_prefix": setting_prefix,
    }
    return cast(
        "type[NetixLimitOffsetPagination]",
        type(name or "GeneratedLimitOffsetPagination", (NetixLimitOffsetPagination,), attrs),
    )


class BaseLimitOffsetPagination(NetixLimitOffsetPagination):
    """The fleet default: 1000/1000, overridable via NETIX_PAGINATION_{DEFAULT,MAX}_LIMIT."""

    setting_prefix: ClassVar[str | None] = "NETIX_PAGINATION"


class BigLimitOffsetPagination(NetixLimitOffsetPagination):
    """Bulk-export paginator, overridable via NETIX_BIG_PAGINATION_{DEFAULT,MAX}_LIMIT."""

    setting_prefix: ClassVar[str | None] = "NETIX_BIG_PAGINATION"
    default_limit = 2000
    max_limit: int | None = 10000
