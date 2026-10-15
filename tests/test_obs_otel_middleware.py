"""The async-capable OTel middleware: no sync island in the ASGI chain, and a hang-up never wedges the worker."""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
import time
from collections.abc import Iterator
from typing import Any
from unittest import mock

import pytest
from asgiref.sync import AsyncToSync
from django.core.handlers.asgi import ASGIHandler
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.instrumentation.django.middleware.otel_middleware import _DjangoMiddleware
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from netix_backend.asgi.testing import HangUpCycle, ServerCycle, django_scope
from netix_backend.observability import otel
from netix_backend.observability.otel_middleware import (
    ASYNC_MIDDLEWARE,
    CLIENT_CLOSED_REQUEST,
    UPSTREAM_MIDDLEWARE,
    AsyncDjangoMiddleware,
    use_async_middleware,
)
from tests import otel_urls

BASE_MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
# Stands in for the lazy URLconf import that was running inside the middleware when kubelet hung up on 527dr.
SYNC_PHASE_SECONDS = 0.2
HANG_UP_AFTER = 0.05
# A wedged worker never comes back; this only bounds how long the test waits to say so.
WEDGE_DEADLINE = 10.0


@pytest.fixture
def traced(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """OTel installed through configure() itself, recording spans in memory and leaving no global provider behind."""
    for name in [name for name in os.environ if name.startswith(("OTEL_", "NETIX_OTEL"))]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    otel_urls.cancelled.clear()
    otel.reset_for_tests()
    with (
        override_settings(MIDDLEWARE=list(BASE_MIDDLEWARE), ROOT_URLCONF="tests.otel_urls"),
        mock.patch("opentelemetry.instrumentation.logging.LoggingInstrumentor"),
        mock.patch.object(otel, "_instrument_optional"),
    ):
        assert otel.configure("svc") is True
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(_DjangoMiddleware, "_tracer", provider.get_tracer(__name__))
        monkeypatch.setattr(_DjangoMiddleware, "_active_request_counter", mock.Mock())
        try:
            yield exporter
        finally:
            DjangoInstrumentor().uninstrument()
            otel.reset_for_tests()


def active_requests() -> int:
    """Net value of the active-request gauge; a request that never closes its span leaves it above zero."""
    counter: Any = _DjangoMiddleware._active_request_counter
    return sum(call.args[0] for call in counter.add.call_args_list)


def status_of(span: ReadableSpan) -> Any:
    attributes = span.attributes or {}
    return attributes.get("http.response.status_code", attributes.get("http.status_code"))


def spans_by_path(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {str((span.attributes or {}).get("http.target", span.name)): span for span in exporter.get_finished_spans()}


@pytest.fixture
def bridges() -> Iterator[list[str]]:
    """Every async_to_sync bridge built while the test runs: each one is a sync island re-entering the loop."""
    built: list[str] = []
    original = AsyncToSync.__init__

    def spy(self: AsyncToSync, awaitable: Any, *args: Any, **kwargs: Any) -> None:
        built.append(getattr(awaitable, "__qualname__", repr(awaitable)))
        original(self, awaitable, *args, **kwargs)

    with mock.patch.object(AsyncToSync, "__init__", spy):
        yield built


def serve(application: Any, path: str) -> list[int]:
    cycle = ServerCycle()
    asyncio.run(application(django_scope(path), cycle.receive, cycle.send))
    return cycle.statuses


class TestConfigureInstallsIt:
    def test_replaces_the_upstream_entry_in_place(self, traced: InMemorySpanExporter) -> None:
        from django.conf import settings

        assert settings.MIDDLEWARE == [ASYNC_MIDDLEWARE, *BASE_MIDDLEWARE]
        assert UPSTREAM_MIDDLEWARE not in settings.MIDDLEWARE

    def test_leaves_middleware_alone_when_django_instrumentation_is_switched_off(
        self, monkeypatch: pytest.MonkeyPatch, traced: InMemorySpanExporter
    ) -> None:
        from django.conf import settings

        DjangoInstrumentor().uninstrument()
        otel.reset_for_tests()
        monkeypatch.setenv("OTEL_PYTHON_DJANGO_INSTRUMENT", "False")
        with override_settings(MIDDLEWARE=list(BASE_MIDDLEWARE)):
            assert otel.configure("svc") is True
            assert settings.MIDDLEWARE == BASE_MIDDLEWARE

    def test_leaves_unconfigured_settings_untouched(self) -> None:
        """OTEL_PYTHON_DJANGO_INSTRUMENT=False skips upstream's settings read; ours must not force a settings import."""
        from django.conf import LazySettings

        with mock.patch.object(LazySettings, "configured", new_callable=mock.PropertyMock, return_value=False):
            with mock.patch.object(LazySettings, "__setattr__") as write:
                use_async_middleware()
        write.assert_not_called()


class TestNoSyncIsland:
    """The 2026-08-23 and 2026-10-15 wedges both needed a sync-only middleware above an async chain."""

    def test_async_handler_marks_the_instance_a_coroutine_function(self) -> None:
        async def get_response(request: Any) -> HttpResponse:
            return HttpResponse()

        middleware = AsyncDjangoMiddleware(get_response)
        assert inspect.iscoroutinefunction(middleware) is True
        assert inspect.iscoroutinefunction(middleware.process_view) is True

    def test_sync_handler_keeps_the_sync_path(self) -> None:
        middleware = AsyncDjangoMiddleware(lambda request: HttpResponse())
        assert inspect.iscoroutinefunction(middleware) is False
        assert inspect.iscoroutinefunction(middleware.process_view) is False

    def test_asgi_chain_builds_no_async_to_sync_bridge(self, traced: InMemorySpanExporter, bridges: list[str]) -> None:
        from django.core.asgi import get_asgi_application

        assert serve(get_asgi_application(), "/fast/") == [200]
        assert bridges == [], f"async_to_sync bridges built with OTel on: {bridges}"

    def test_upstream_middleware_would_build_one(self, traced: InMemorySpanExporter, bridges: list[str]) -> None:
        """Keeps the check above honest: the class we replace really does make Django bridge the chain."""
        with override_settings(MIDDLEWARE=[UPSTREAM_MIDDLEWARE, *BASE_MIDDLEWARE]):
            ASGIHandler()
        assert bridges, "Django no longer bridges a sync-only middleware; revisit AsyncDjangoMiddleware"


class TestTracing:
    def test_async_request_is_traced_with_its_route(self, traced: InMemorySpanExporter) -> None:
        from django.core.asgi import get_asgi_application

        assert serve(get_asgi_application(), "/fast/") == [200]
        (span,) = traced.get_finished_spans()
        assert status_of(span) == 200
        # Set by process_view, so this proves the async variant ran without a thread hop.
        assert (span.attributes or {}).get("http.route") == "fast/"
        assert active_requests() == 0

    def test_sync_path_is_upstreams_own(self, traced: InMemorySpanExporter) -> None:
        middleware = AsyncDjangoMiddleware(lambda request: HttpResponse(status=201))
        response = middleware(RequestFactory().get("/fast/"))
        assert isinstance(response, HttpResponse)
        assert response.status_code == 201
        (span,) = traced.get_finished_spans()
        assert status_of(span) == 201
        assert active_requests() == 0

    async def test_cancellation_survives_a_failing_span_close(
        self, traced: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Swapping the CancelledError for the close's own error would break Django's TaskGroup unwind."""

        async def cancelled(request: Any) -> HttpResponse:
            raise asyncio.CancelledError

        def broken(self: Any, request: Any, response: Any) -> Any:
            raise RuntimeError("span close failed")

        monkeypatch.setattr(_DjangoMiddleware, "process_response", broken)
        with pytest.raises(asyncio.CancelledError):
            await AsyncDjangoMiddleware(cancelled).__acall__(RequestFactory().get("/fast/"))
        assert "could not close the span of a cancelled request" in caplog.text


class TestHangUp:
    """kubelet hanging up on a probe mid-request is what cancelled the request inside the island on 527dr."""

    def hang_up_then_retry(self) -> dict[str, Any]:
        """Hang up on /slow/, then ask for /fast/, on a loop off this thread so a wedge fails instead of hanging."""
        from django.core.asgi import get_asgi_application

        application = get_asgi_application()
        result: dict[str, Any] = {}

        async def scenario() -> None:
            gaps: list[float] = []

            async def heartbeat() -> None:
                last = time.monotonic()
                while True:
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    gaps.append(now - last)
                    last = now

            beat = asyncio.create_task(heartbeat())
            hung_up = HangUpCycle(HANG_UP_AFTER)
            started = time.monotonic()
            await application(django_scope("/slow/"), hung_up.receive, hung_up.send)
            result["released_after"] = time.monotonic() - started
            result["hung_up_statuses"] = hung_up.statuses
            retry = ServerCycle()
            await application(django_scope("/fast/"), retry.receive, retry.send)
            result["retry_statuses"] = retry.statuses
            beat.cancel()
            result["worst_gap"] = max(gaps)

        worker = threading.Thread(target=asyncio.run, args=(scenario(),), daemon=True)
        worker.start()
        worker.join(WEDGE_DEADLINE)
        assert not worker.is_alive(), "the worker wedged after a hang-up"
        return result

    @pytest.fixture(autouse=True)
    def slow_sync_phase(self, monkeypatch: pytest.MonkeyPatch, traced: InMemorySpanExporter) -> None:
        """Sync work inside the middleware when the hang-up lands: the island ran exactly this in a thread."""
        monkeypatch.setattr(
            _DjangoMiddleware, "_otel_request_hook", lambda span, request: threading.Event().wait(SYNC_PHASE_SECONDS)
        )

    def test_worker_keeps_serving(self) -> None:
        result = self.hang_up_then_retry()
        assert result["hung_up_statuses"] == []
        assert result["retry_statuses"] == [200]

    def test_cancellation_reaches_the_view(self) -> None:
        """Under the island the hang-up was swallowed and the abandoned view ran to its end on a thread."""
        result = self.hang_up_then_retry()
        assert otel_urls.cancelled == ["/slow/"]
        assert result["released_after"] < otel_urls.SLOW_SECONDS

    def test_event_loop_never_blocks_on_the_abandoned_request(self) -> None:
        """asgiref < 3.12 joined the island's thread from the loop; nothing here may hold the loop that long."""
        result = self.hang_up_then_retry()
        assert result["worst_gap"] < otel_urls.SLOW_SECONDS / 2

    def test_cancelled_span_ends_as_client_closed(self, traced: InMemorySpanExporter) -> None:
        """Upstream never closes it: the span is lost and the active-request gauge climbs on every hang-up."""
        self.hang_up_then_retry()
        spans = spans_by_path(traced)
        assert status_of(spans["/slow/"]) == CLIENT_CLOSED_REQUEST
        assert status_of(spans["/fast/"]) == 200
        assert active_requests() == 0
