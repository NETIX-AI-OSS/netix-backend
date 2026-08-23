"""Health endpoints: an ASGI interceptor that answers below Django, plus the plain Django view."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

HEALTHZ_PATH = "/healthz/"
HEALTHZ_BODY = b'{"status": "ok"}'

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[Any]]
Send = Callable[[Any], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


async def send_healthz(send: Send, body: bytes = HEALTHZ_BODY) -> None:
    """Write the health response directly to the ASGI ``send`` channel."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def healthz_asgi(app: ASGIApp, path: str = HEALTHZ_PATH, *, body: bytes = HEALTHZ_BODY) -> ASGIApp:
    """Wrap *app* so probes are answered above Django — the only variant that still replies during a DB outage."""
    paths = {path, path.rstrip("/")}

    async def health_intercepted(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in paths:
            await send_healthz(send, body)
            return
        await app(scope, receive, send)

    return health_intercepted


def healthz_view(request: Any) -> Any:
    """Django view twin of :func:`healthz_asgi` for services routing health through ``urls.py``."""
    from django.http import JsonResponse  # local import: netix_backend.http must load without Django configured

    return JsonResponse({"status": "ok"})


__all__ = ("HEALTHZ_BODY", "HEALTHZ_PATH", "healthz_asgi", "healthz_view", "send_healthz")
