"""Health endpoints: the ASGI interceptor that answers above Django, and the plain view."""

import json

from netix_backend.http import health


class _Recorder:
    def __init__(self):
        self.messages = []
        self.forwarded = []

    async def send(self, message):
        self.messages.append(message)

    async def app(self, scope, receive, send):
        self.forwarded.append(scope)

    async def receive(self):  # pragma: no cover - the interceptor never pulls from the channel
        return {"type": "http.request"}


async def _call(app, scope, recorder):
    await app(scope, recorder.receive, recorder.send)


async def test_healthz_asgi_answers_without_touching_the_app():
    recorder = _Recorder()
    app = health.healthz_asgi(recorder.app)
    await _call(app, {"type": "http", "path": "/healthz/"}, recorder)
    start, body = recorder.messages
    assert start["status"] == 200
    assert (b"content-type", b"application/json") in start["headers"]
    assert (b"content-length", b"16") in start["headers"]
    assert json.loads(body["body"]) == {"status": "ok"}
    assert recorder.forwarded == []


async def test_healthz_asgi_accepts_the_unslashed_form():
    recorder = _Recorder()
    await _call(health.healthz_asgi(recorder.app), {"type": "http", "path": "/healthz"}, recorder)
    assert recorder.messages and recorder.forwarded == []


async def test_healthz_asgi_forwards_everything_else():
    recorder = _Recorder()
    app = health.healthz_asgi(recorder.app)
    await _call(app, {"type": "http", "path": "/api/things/"}, recorder)
    await _call(app, {"type": "lifespan"}, recorder)
    assert recorder.messages == []
    assert len(recorder.forwarded) == 2


async def test_healthz_asgi_honours_a_custom_path_and_body():
    recorder = _Recorder()
    app = health.healthz_asgi(recorder.app, "/livez/", body=b"alive")
    await _call(app, {"type": "http", "path": "/healthz/"}, recorder)
    assert recorder.forwarded
    await _call(app, {"type": "http", "path": "/livez/"}, recorder)
    assert recorder.messages[1]["body"] == b"alive"


def test_healthz_view_returns_the_shared_envelope(rf):
    response = health.healthz_view(rf.get("/healthz/"))
    assert response.status_code == 200
    assert json.loads(response.content) == {"status": "ok"}
