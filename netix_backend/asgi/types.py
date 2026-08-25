"""ASGI/WSGI callable aliases shared by the wrappers in this package; no Django, no third-party imports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

Message = Mapping[str, Any]
Scope = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

WSGIEnvironment = dict[str, Any]
StartResponse = Callable[..., Any]
WSGIApp = Callable[[WSGIEnvironment, StartResponse], Iterable[bytes]]

__all__ = (
    "ASGIApp",
    "Message",
    "Receive",
    "Scope",
    "Send",
    "StartResponse",
    "WSGIApp",
    "WSGIEnvironment",
)
