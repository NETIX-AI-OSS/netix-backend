"""The lifespan handshake Django does not implement; the bug it fixes is one Sentry event per worker boot."""

import pytest

from netix_backend.asgi.lifespan import LifespanWrapper, serve_lifespan, wrap


class Channel:
    """Minimal ASGI receive/send pair: receive drains a scripted queue, send records."""

    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def receive(self):
        if not self.incoming:
            raise AssertionError("the shim asked for a message after shutdown was acknowledged")
        return self.incoming.pop(0)

    async def send(self, message):
        self.sent.append(message)


class RecordingApp:
    """Stands in for Django's ASGIHandler, which raises ValueError on any scope it is not given."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append((scope, receive, send))
        await send({"type": "http.response.start", "status": 200})


@pytest.mark.parametrize("form", [wrap, LifespanWrapper])
async def test_startup_is_acknowledged(form):
    channel = Channel([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    await form(RecordingApp())({"type": "lifespan"}, channel.receive, channel.send)
    assert channel.sent[0] == {"type": "lifespan.startup.complete"}


@pytest.mark.parametrize("form", [wrap, LifespanWrapper])
async def test_shutdown_is_acknowledged_and_returns(form):
    channel = Channel([{"type": "lifespan.shutdown"}])
    await form(RecordingApp())({"type": "lifespan"}, channel.receive, channel.send)
    assert channel.sent == [{"type": "lifespan.shutdown.complete"}]


@pytest.mark.parametrize("form", [wrap, LifespanWrapper])
async def test_unknown_lifespan_messages_are_ignored(form):
    """A newer server may send messages we do not know; ignoring them must not break the handshake."""
    channel = Channel([{"type": "lifespan.something.new"}, {"type": "lifespan.shutdown"}])
    await form(RecordingApp())({"type": "lifespan"}, channel.receive, channel.send)
    assert channel.sent == [{"type": "lifespan.shutdown.complete"}]


@pytest.mark.parametrize("form", [wrap, LifespanWrapper])
async def test_django_never_sees_the_lifespan_scope(form):
    """ASGIHandler raising ValueError here is what sentry-sdk escalated into a GlitchTip issue."""
    app = RecordingApp()
    channel = Channel([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    await form(app)({"type": "lifespan"}, channel.receive, channel.send)
    assert app.calls == []


@pytest.mark.parametrize("form", [wrap, LifespanWrapper])
@pytest.mark.parametrize("scope_type", ["http", "websocket"])
async def test_other_scopes_are_forwarded_untouched(form, scope_type):
    app = RecordingApp()
    channel = Channel([])
    scope = {"type": scope_type, "path": "/gateways/"}
    await form(app)(scope, channel.receive, channel.send)
    assert app.calls == [(scope, channel.receive, channel.send)]
    assert channel.sent == [{"type": "http.response.start", "status": 200}]


async def test_serve_lifespan_can_be_driven_directly():
    """tag-service fuses the handshake with its own healthz short-circuit, so the primitive stays public."""
    channel = Channel([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    await serve_lifespan(channel.receive, channel.send)
    assert channel.sent == [{"type": "lifespan.startup.complete"}, {"type": "lifespan.shutdown.complete"}]
