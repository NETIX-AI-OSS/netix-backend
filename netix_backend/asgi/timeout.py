"""Hard wall-clock ceiling on request handling, applied outside the Django handler."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from netix_backend.asgi.types import ASGIApp, Message, Receive, Scope, Send
from netix_backend.env import env_float, env_int

logger = logging.getLogger(__name__)

# A backstop against a wedged worker, not a latency budget: set it well above the slowest healthy safe request.
REQUEST_TIMEOUT_SECONDS = env_float("REQUEST_TIMEOUT_SECONDS", 120.0)

# uvicorn's --limit-concurrency counts only watched work, so abandoned requests are bounded here.
MAX_ABANDONED_REQUESTS = env_int("MAX_ABANDONED_REQUESTS", 50)

RETRY_AFTER_SECONDS = 5

# Safe methods only: a 504 is retryable, so answering one mid-write turns one write into two.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Preflights are answered before any view runs, and shedding one fails the write it was clearing.
NEVER_SHED = frozenset({"OPTIONS"})

TIMEOUT_MESSAGE = "Request exceeded the server time limit."
OVERLOADED_MESSAGE = "Server is shedding load; retry shortly."

ErrorRenderer = Callable[[int, str, Scope, int | None], tuple[Message, Message]]


def django_setting(name: str, default: Any = None) -> Any:
    """Read a Django setting lazily, yielding *default* while the project is unconfigured."""
    # Imported here, never at module level, so this package loads in a pre-django.setup() boot path.
    from django.conf import settings

    if not settings.configured:
        return default
    return getattr(settings, name, default)


def _request_origin(scope: Scope) -> bytes | None:
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for name, value in headers:
        if name == b"origin":
            return value
    return None


def cors_headers(scope: Scope) -> list[tuple[bytes, bytes]]:
    """Reproduce what CorsMiddleware would have added; a reply sent from here never reaches it."""
    origin = _request_origin(scope)
    if not origin:
        return []
    allow_all = django_setting("CORS_ALLOW_ALL_ORIGINS", False)
    if not allow_all and origin.decode("latin-1") not in django_setting("CORS_ALLOWED_ORIGINS", ()):
        return []
    headers = [(b"access-control-allow-origin", b"*" if allow_all else origin), (b"vary", b"Origin")]
    if not allow_all and django_setting("CORS_ALLOW_CREDENTIALS", False):
        headers.append((b"access-control-allow-credentials", b"true"))
    return headers


def error_messages(status: int, message: str, scope: Scope, retry_after: int | None = None) -> tuple[Message, Message]:
    """The ASGI message pair for an error shaped like custom_exception_handler's envelope."""
    # `messages` is a bare string here, unlike the list the DRF handler flattens; the fleet's clients read both.
    body = json.dumps({"status_code": status, "messages": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode()))
    headers.extend(cors_headers(scope))
    return (
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body},
    )


def text_messages(status: int, message: str, scope: Scope, retry_after: int | None = None) -> tuple[Message, Message]:
    """The pre-envelope text/plain reply cafm-backend and simulator-service answer with today."""
    headers = [(b"content-type", b"text/plain; charset=utf-8")]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode()))
    return (
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": message.encode()},
    )


# The wrapped app's ``send``, muted once we have answered on its behalf.
class _ResponseChannel:
    def __init__(self, send: Send) -> None:
        self._send = send
        self.started = False
        self.abandoned = False

    async def send(self, message: Message) -> None:
        if self.abandoned:
            return
        if message["type"] == "http.response.start":
            self.started = True
        await self._send(message)


# ASGI middleware: answer 504 for any HTTP request still unanswered past the timeout.
class RequestTimeoutMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        timeout: float | None = None,
        max_abandoned: float | None = None,
        *,
        safe_methods: frozenset[str] | None = SAFE_METHODS,
        never_shed: frozenset[str] = NEVER_SHED,
        timeout_setting: str | None = None,
        retry_after: int | None = RETRY_AFTER_SECONDS,
        error_body: ErrorRenderer = error_messages,
        timeout_message: str = TIMEOUT_MESSAGE,
        overloaded_message: str = OVERLOADED_MESSAGE,
    ) -> None:
        self.app = app
        # Resolved at construction, not import, so the ceiling stays patchable and django.setup() is never raced.
        if timeout is None and timeout_setting is not None:
            timeout = django_setting(timeout_setting)
        self.timeout = REQUEST_TIMEOUT_SECONDS if timeout is None else float(timeout)
        self.max_abandoned = MAX_ABANDONED_REQUESTS if max_abandoned is None else max_abandoned
        # None applies the ceiling to every method; an empty set applies it to none, disabling the middleware.
        self.safe_methods = safe_methods
        self.never_shed = never_shed
        self.retry_after = retry_after
        self.error_body = error_body
        self.timeout_message = timeout_message
        self.overloaded_message = overloaded_message
        self._abandoned: set[asyncio.Task[None]] = set()

    def is_capped(self, method: str | None) -> bool:
        """Whether the ceiling applies to *method* under this instance's ``safe_methods``."""
        return self.safe_methods is None or method in self.safe_methods

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = scope.get("method")
        if scope["type"] != "http" or not self.is_capped(method):
            await self.app(scope, receive, send)
            return

        if method not in self.never_shed and len(self._abandoned) >= self.max_abandoned:
            logger.warning(
                "Shedding %s: %s abandoned requests still in flight", scope.get("path", ""), len(self._abandoned)
            )
            await self._reply(send, self.error_body(503, self.overloaded_message, scope, self.retry_after))
            return

        channel = _ResponseChannel(send)
        request = asyncio.ensure_future(self.app(scope, receive, channel.send))
        done, _ = await asyncio.wait({request}, timeout=self.timeout)

        if request in done:
            request.result()  # re-raise whatever the app raised, for the server to handle
            return

        if channel.started:
            # Status line already on the wire, so a 504 now would be a protocol error.
            await request
            return

        channel.abandoned = True
        # Answering makes the server report a disconnect, so Django cancels it; asgiref still waits out the thread.
        self._abandon(request, scope)
        await self._reply(send, self.error_body(504, self.timeout_message, scope, None))

    @staticmethod
    async def _reply(send: Send, messages: tuple[Message, Message]) -> None:
        for message in messages:
            await send(message)

    def _abandon(self, request: asyncio.Task[None], scope: Scope) -> None:
        """Let the request finish unwatched, holding a reference so it is not garbage collected."""
        self._abandoned.add(request)
        path = scope.get("path", "")

        def log_late_finish(finished: asyncio.Task[None]) -> None:
            self._abandoned.discard(finished)
            # Retrieved so asyncio does not report it as an unhandled task exception.
            error = None if finished.cancelled() else finished.exception()
            logger.warning("Abandoned %s after %ss timeout; it finished later (error=%r)", path, self.timeout, error)

        request.add_done_callback(log_late_finish)


__all__ = (
    "MAX_ABANDONED_REQUESTS",
    "NEVER_SHED",
    "OVERLOADED_MESSAGE",
    "REQUEST_TIMEOUT_SECONDS",
    "RETRY_AFTER_SECONDS",
    "SAFE_METHODS",
    "TIMEOUT_MESSAGE",
    "ErrorRenderer",
    "RequestTimeoutMiddleware",
    "cors_headers",
    "django_setting",
    "error_messages",
    "text_messages",
)
