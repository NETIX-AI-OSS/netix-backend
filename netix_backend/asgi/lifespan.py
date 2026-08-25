"""Answer the ASGI lifespan protocol outside Django, so no lifespan scope ever reaches ASGIHandler."""

from __future__ import annotations

from netix_backend.asgi.types import ASGIApp, Receive, Scope, Send


async def serve_lifespan(receive: Receive, send: Send) -> None:
    """Complete the startup/shutdown handshake and return once the server shuts down."""
    # Unknown message types are ignored rather than answered with lifespan.startup.failed, as every copy does today.
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


def wrap(app: ASGIApp) -> ASGIApp:
    """Return an ASGI app that handles lifespan itself and forwards every other scope untouched."""

    async def application(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await serve_lifespan(receive, send)
            return
        await app(scope, receive, send)

    return application


# The call form eight repos already spell as LifespanWrapper(app); identical semantics to wrap().
class LifespanWrapper:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await serve_lifespan(receive, send)
            return
        await self.app(scope, receive, send)


__all__ = ("LifespanWrapper", "serve_lifespan", "wrap")
