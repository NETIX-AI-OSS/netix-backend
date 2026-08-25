"""The shared contract suite for the ASGI request ceiling: subclass RequestTimeoutContract and set the knobs."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Sequence
from typing import Any, ClassVar, NamedTuple

import pytest

from netix_backend.asgi.timeout import (
    OVERLOADED_MESSAGE,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_AFTER_SECONDS,
    SAFE_METHODS,
    TIMEOUT_MESSAGE,
    RequestTimeoutMiddleware,
)
from netix_backend.asgi.types import ASGIApp, Message, Receive, Scope, Send

SLOW_VIEW_SECONDS = 1.0
ALLOWED_ORIGIN = b"https://cafm.netixai.dev"
UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
Headers = Sequence[tuple[bytes, bytes]]


# What the server saw for one request.
class Reply(NamedTuple):
    status: int
    headers: dict[bytes, bytes]
    body: bytes
    finished: asyncio.Event


def slow_app(delay: float, finished: asyncio.Event) -> ASGIApp:
    """An app that answers 200 after *delay*, setting *finished* once it has."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        # Reads the request the way a real handler does, so the ceiling is measured over both channels.
        await receive()
        await asyncio.sleep(delay)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        finished.set()

    return app


async def drive(app: ASGIApp, method: str, finished: asyncio.Event, headers: Headers = ()) -> Reply:
    """Push one request through *app* and collect what reached the server."""
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request"}

    scope = {"type": "http", "method": method, "path": "/", "headers": list(headers)}
    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return Reply(start["status"], dict(start["headers"]), body, finished)


def django_scope(path: str, method: str = "GET") -> Scope:
    """A scope complete enough for Django's ASGIHandler, unlike the trimmed one :func:`drive` builds."""
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "http_version": "1.1",
        "scheme": "http",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8001),
        "asgi": {"version": "3.0"},
    }


def slow_view(request: Any) -> Any:
    """A sync view that pins its worker thread; route it from the module a suite names in ``urlconf``."""
    from django.http import HttpResponse

    # Event.wait, not time.sleep: it blocks the executor thread the same way but no conftest mocks it.
    threading.Event().wait(SLOW_VIEW_SECONDS)
    return HttpResponse("done")


def assert_no_timeout_middleware_entry(needle: str = "timeout") -> None:
    """Opt-in: the wait_for middleware must never come back; the ceiling belongs in asgi.py instead."""
    from django.conf import settings

    assert not any(needle in entry.lower() for entry in settings.MIDDLEWARE)


def assert_ceiling_clears_statement_timeout(ceiling_seconds: float, statement_timeout_ms: float) -> None:
    """Opt-in: a DB-bound request must surface its own statement_timeout error, not an opaque 504."""
    from netix_backend.django.testing import assert_timeout_invariant

    assert_timeout_invariant(request_timeout_seconds=ceiling_seconds, statement_timeout_ms=int(statement_timeout_ms))


# uvicorn's HTTP cycle: completing a response wakes the pending receive() with http.disconnect.
class ServerCycle:
    def __init__(self) -> None:
        self.message_event = asyncio.Event()
        self.message_event.set()
        self.response_complete = False
        self.statuses: list[int] = []

    async def receive(self) -> Message:
        if not self.response_complete:
            await self.message_event.wait()
            self.message_event.clear()
        if self.response_complete:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.statuses.append(message["status"])
        elif not message.get("more_body"):
            self.response_complete = True
            self.message_event.set()


# Subclass this in a service's test module as `class TestRequestTimeout(RequestTimeoutContract): pass`.
class RequestTimeoutContract:
    middleware: ClassVar[type[RequestTimeoutMiddleware]] = RequestTimeoutMiddleware
    # Dotted path whose REQUEST_TIMEOUT_SECONDS the middleware reads, for the default-ceiling check.
    ceiling_module: ClassVar[str] = "netix_backend.asgi.timeout"
    ceiling: ClassVar[float] = REQUEST_TIMEOUT_SECONDS
    safe_methods: ClassVar[Sequence[str]] = tuple(sorted(SAFE_METHODS))
    unsafe_methods: ClassVar[Sequence[str]] = UNSAFE_METHODS
    never_shed_method: ClassVar[str] = "OPTIONS"
    timeout_message: ClassVar[str] = TIMEOUT_MESSAGE
    overloaded_message: ClassVar[str] = OVERLOADED_MESSAGE
    retry_after_seconds: ClassVar[int] = RETRY_AFTER_SECONDS
    allowed_origin: ClassVar[bytes] = ALLOWED_ORIGIN
    # Opt-in knobs: each guards a check that only some repos can make.
    urlconf: ClassVar[str | None] = None
    forbid_middleware_entry: ClassVar[bool] = False
    db_statement_timeout_ms: ClassVar[float | None] = None

    def build(self, app: ASGIApp, **kwargs: Any) -> RequestTimeoutMiddleware:
        """The middleware under test; override to pass a service's own construction knobs."""
        return self.middleware(app, **kwargs)

    def run_request(self, method: str, delay: float, headers: Headers = (), **kwargs: Any) -> Reply:
        """Drive one request through the ceiling over an app that answers after *delay*."""

        async def scenario() -> Reply:
            finished = asyncio.Event()
            return await drive(self.build(slow_app(delay, finished), **kwargs), method, finished, headers)

        return asyncio.run(scenario())

    def test_safe_methods_over_the_ceiling_return_504(self) -> None:
        for method in self.safe_methods:
            assert self.run_request(method, delay=1, timeout=0.05).status == 504

    def test_timed_out_request_is_abandoned_not_cancelled(self) -> None:
        """Cancelling would tear down ThreadSensitiveContext on the event loop, freezing every request."""

        async def scenario(method: str) -> None:
            finished = asyncio.Event()
            reply = await drive(self.build(slow_app(0.2, finished), timeout=0.05), method, finished)
            assert reply.status == 504
            # The abandoned request must still run to completion after the 504 went out.
            await asyncio.wait_for(reply.finished.wait(), timeout=5)

        for method in self.safe_methods:
            asyncio.run(scenario(method))

    def test_unsafe_methods_are_never_cut_short(self) -> None:
        """A 504 is retryable; unsafe methods are bounded by statement_timeout at the database instead."""
        for method in self.unsafe_methods:
            reply = self.run_request(method, delay=0.2, timeout=0.05)
            assert reply.status == 200
            assert reply.finished.is_set() is True

    def test_safe_method_under_the_ceiling_passes_through(self) -> None:
        reply = self.run_request("GET", delay=0, timeout=0.05)
        assert reply.status == 200
        assert reply.finished.is_set() is True

    def test_a_started_response_is_never_replaced_with_a_504(self) -> None:
        """Once the status line is on the wire a 504 would be a protocol error, so the reply is waited out."""

        async def scenario() -> Reply:
            finished = asyncio.Event()

            async def app(scope: Scope, receive: Receive, send: Send) -> None:
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await asyncio.sleep(0.2)
                await send({"type": "http.response.body", "body": b"ok"})
                finished.set()

            return await drive(self.build(app, timeout=0.05), "GET", finished)

        reply = asyncio.run(scenario())
        assert reply.status == 200
        assert reply.finished.is_set() is True

    def test_non_http_scope_passes_through(self) -> None:
        """Lifespan and websocket scopes must not be wrapped in a timeout, nor have their channels replaced."""
        received = []
        sent = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            received.append((scope["type"], await receive()))
            await send({"type": "lifespan.startup.complete"})

        async def receive() -> Message:
            return {"type": "lifespan.startup"}

        async def send(message: Message) -> None:
            sent.append(message)

        asyncio.run(self.build(app)({"type": "lifespan"}, receive, send))
        assert received == [("lifespan", {"type": "lifespan.startup"})]
        assert sent == [{"type": "lifespan.startup.complete"}]

    def test_ceiling_defaults_to_the_module_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(f"{self.ceiling_module}.REQUEST_TIMEOUT_SECONDS", 1.5)
        assert self.build(slow_app(0, asyncio.Event())).timeout == 1.5

    def test_timeout_reply_uses_the_error_envelope(self) -> None:
        """A reply sent from here never reaches DRF, so it has to build the envelope itself."""
        reply = self.run_request("GET", delay=0.2, timeout=0.05)
        assert reply.headers[b"content-type"] == b"application/json"
        assert reply.headers[b"content-length"] == str(len(reply.body)).encode()
        assert json.loads(reply.body) == {"status_code": 504, "messages": self.timeout_message}

    def test_timeout_reply_carries_cors_headers(self) -> None:
        """Without them a browser reports the 504 as a CORS failure; CorsMiddleware is inside Django."""
        from django.test import override_settings

        with override_settings(
            CORS_ALLOW_ALL_ORIGINS=False,
            CORS_ALLOWED_ORIGINS=[self.allowed_origin.decode()],
            CORS_ALLOW_CREDENTIALS=True,
        ):
            reply = self.run_request("GET", delay=0.2, headers=[(b"origin", self.allowed_origin)], timeout=0.05)
        assert reply.status == 504
        assert reply.headers[b"access-control-allow-origin"] == self.allowed_origin
        assert reply.headers[b"access-control-allow-credentials"] == b"true"
        assert reply.headers[b"vary"] == b"Origin"

    def test_unlisted_origin_gets_no_cors_headers(self) -> None:
        from django.test import override_settings

        with override_settings(CORS_ALLOW_ALL_ORIGINS=False, CORS_ALLOWED_ORIGINS=[self.allowed_origin.decode()]):
            reply = self.run_request(
                "GET", delay=0.2, headers=[(b"origin", b"https://elsewhere.example")], timeout=0.05
            )
        assert b"access-control-allow-origin" not in reply.headers

    def test_allow_all_origins_answers_with_a_wildcard(self) -> None:
        from django.test import override_settings

        with override_settings(CORS_ALLOW_ALL_ORIGINS=True, CORS_ALLOW_CREDENTIALS=False):
            reply = self.run_request("GET", delay=0.2, headers=[(b"origin", b"https://anywhere.example")], timeout=0.05)
        assert reply.headers[b"access-control-allow-origin"] == b"*"
        assert b"access-control-allow-credentials" not in reply.headers

    def test_request_without_an_origin_gets_no_cors_headers(self) -> None:
        reply = self.run_request("GET", delay=0.2, timeout=0.05)
        assert b"access-control-allow-origin" not in reply.headers

    def test_worker_sheds_once_the_abandoned_pool_is_full(self) -> None:
        """uvicorn's --limit-concurrency counts only watched work, so nothing else bounds this."""

        async def scenario() -> Reply:
            finished = asyncio.Event()
            middleware = self.build(slow_app(2, finished), timeout=0.05, max_abandoned=1)
            assert (await drive(middleware, "GET", finished)).status == 504
            return await drive(middleware, "GET", finished)

        reply = asyncio.run(scenario())
        assert reply.status == 503
        assert reply.headers[b"retry-after"] == str(self.retry_after_seconds).encode()
        assert json.loads(reply.body) == {"status_code": 503, "messages": self.overloaded_message}

    def test_preflights_are_never_shed(self) -> None:
        """A shed preflight fails the write it was clearing, and writes are never timed out here."""

        async def scenario() -> Reply:
            finished = asyncio.Event()
            middleware = self.build(slow_app(0, finished), timeout=0.05, max_abandoned=0)
            return await drive(middleware, self.never_shed_method, finished)

        assert asyncio.run(scenario()).status == 200

    def test_shedding_stops_once_the_abandoned_work_drains(self) -> None:
        """A pool that never releases would pin the worker at 503 for the rest of its life."""
        delays = [0.2, 0.0]

        async def scenario() -> Reply:
            finished = asyncio.Event()

            async def app(scope: Scope, receive: Receive, send: Send) -> None:
                await asyncio.sleep(delays.pop(0))
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})
                finished.set()

            middleware = self.build(app, timeout=0.05, max_abandoned=1)
            assert (await drive(middleware, "GET", finished)).status == 504
            await asyncio.wait_for(finished.wait(), timeout=5)
            # The done callback that releases the pool slot runs on the next loop pass.
            await asyncio.sleep(0)
            return await drive(middleware, "GET", finished)

        assert asyncio.run(scenario()).status == 200

    def test_event_loop_survives_a_timed_out_sync_view(self) -> None:
        """The 2026-08-13 wedge: unwinding early ran ThreadSensitiveContext.__aexit__ on the event loop."""
        if self.urlconf is None:
            pytest.skip("set urlconf to a module routing slow_view to run the event-loop check")
        from django.core.asgi import get_asgi_application
        from django.test import override_settings

        with override_settings(ROOT_URLCONF=self.urlconf, MIDDLEWARE=[]):
            statuses, worst_gap = asyncio.run(self._pump_django(get_asgi_application()))
        assert statuses == [504]
        assert worst_gap < SLOW_VIEW_SECONDS / 2

    async def _pump_django(self, application: ASGIApp) -> tuple[list[int], float]:
        """Run one slow sync view through the ceiling while a heartbeat measures event-loop stalls."""
        gaps = []

        async def heartbeat() -> None:
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        cycle = ServerCycle()
        await self.build(application, timeout=0.1)(django_scope("/slow/"), cycle.receive, cycle.send)
        await asyncio.sleep(SLOW_VIEW_SECONDS + 0.3)
        beat.cancel()
        return cycle.statuses, max(gaps)

    def test_no_request_timeout_middleware_installed(self) -> None:
        if not self.forbid_middleware_entry:
            pytest.skip("set forbid_middleware_entry once the MIDDLEWARE entry is gone")
        assert_no_timeout_middleware_entry()

    def test_ceiling_clears_the_statement_timeout(self) -> None:
        if self.db_statement_timeout_ms is None:
            pytest.skip("set db_statement_timeout_ms in services that ship app/db_timeout.py")
        assert_ceiling_clears_statement_timeout(self.ceiling, self.db_statement_timeout_ms)


__all__ = (
    "ALLOWED_ORIGIN",
    "SLOW_VIEW_SECONDS",
    "UNSAFE_METHODS",
    "Reply",
    "RequestTimeoutContract",
    "ServerCycle",
    "assert_ceiling_clears_statement_timeout",
    "assert_no_timeout_middleware_entry",
    "django_scope",
    "drive",
    "slow_app",
    "slow_view",
)
