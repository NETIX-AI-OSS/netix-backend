"""OpenTelemetry's Django middleware made async-capable, so tracing never puts a sync island in an ASGI chain.

Upstream ``_DjangoMiddleware`` (0.65b0, unchanged in 0.66b1) defines only a sync ``__call__`` and no
``async_capable``. Django therefore runs it in a worker thread at the top of the chain and re-enters the event
loop for everything below it through ``async_to_sync``. Hang up on a request while that thread is busy (a probe
timing out during the lazy URLconf import) and asgiref < 3.12 joins the thread from the loop in
``ThreadSensitiveContext.__aexit__`` while the thread waits on the loop: the worker never answers again. That
is the shape of both user-management-auth wedges (2026-08-23 and 2026-10-15).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from inspect import iscoroutinefunction, markcoroutinefunction
from typing import Any

from django.http import HttpRequest, HttpResponse
from opentelemetry.instrumentation.django.middleware.otel_middleware import _DjangoMiddleware

logger = logging.getLogger(__name__)

# nginx's "client closed request": what the span records when a hang-up cancels the request before any response.
CLIENT_CLOSED_REQUEST = 499


def _dotted(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


class AsyncDjangoMiddleware(_DjangoMiddleware):
    """Upstream's middleware with an async path; the sync path is upstream's own, untouched."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[[HttpRequest], Any]) -> None:
        super().__init__(get_response)
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            # load_middleware and convert_exception_to_response read this; unmarked, Django wraps us as sync again.
            markcoroutinefunction(self)
            # Keep upstream's synchronous metadata hook: Sentry wraps process_view in a sync callable.
            # An async replacement would then return an unawaited coroutine through Django's sync adapter.

    def __call__(self, request: HttpRequest) -> HttpResponse | Awaitable[HttpResponse]:
        if self.async_mode:
            return self.__acall__(request)
        response: HttpResponse = super().__call__(request)
        return response

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        # One task, one context on both sides of the await, so the span's attach/detach tokens stay balanced.
        self.process_request(request)
        try:
            response = await self.get_response(request)
        except BaseException as exc:
            # Django turns every Exception below us into a response, so this is cancellation: a hang-up or a probe
            # timeout. Unhandled, the span never ends and the active-request gauge only ever climbs.
            try:
                self.process_exception(request, exc)
                self.process_response(request, HttpResponse(status=CLIENT_CLOSED_REQUEST))
            except Exception:
                # Raising here would swap the CancelledError for this error and break Django's TaskGroup unwind.
                logger.exception("otel: could not close the span of a cancelled request")
            raise
        finished: HttpResponse = self.process_response(request, response)
        return finished


UPSTREAM_MIDDLEWARE = _dotted(_DjangoMiddleware)
ASYNC_MIDDLEWARE = _dotted(AsyncDjangoMiddleware)


def use_async_middleware() -> None:
    """Swap upstream's entry in settings.MIDDLEWARE for ours, in place; call right after DjangoInstrumentor."""
    from django.conf import settings

    # Unconfigured means DjangoInstrumentor bailed before touching settings, so there is nothing to swap.
    if not settings.configured:
        return
    settings.MIDDLEWARE = [ASYNC_MIDDLEWARE if entry == UPSTREAM_MIDDLEWARE else entry for entry in settings.MIDDLEWARE]


__all__ = (
    "ASYNC_MIDDLEWARE",
    "CLIENT_CLOSED_REQUEST",
    "UPSTREAM_MIDDLEWARE",
    "AsyncDjangoMiddleware",
    "use_async_middleware",
)
