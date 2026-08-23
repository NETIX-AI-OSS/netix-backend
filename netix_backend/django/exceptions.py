"""Shared DRF exception handler emitting the NETIX {"status_code", "messages"} envelope."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Any, Final

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DatabaseError, OperationalError
from django.http import Http404
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)

Flattener = Callable[[Any], list[str]]

HTTP404_MESSAGE: Final = "404: Resource not found"
DB_UNAVAILABLE_MESSAGE: Final = "Service temporarily unavailable"
DEFAULT_FLATTENER: Final = "template"


def _message_entries(payload: dict[str, Any]) -> list[str] | None:
    """The `{"message": [...]}` special case shared by four of the five fleet flatteners."""
    message = payload.get("message")
    if not message:
        return None
    if isinstance(message, (list, tuple, set)):
        return [str(item) for item in message]
    return [str(message)]


def flatten_template(obj: Any) -> list[str]:
    """backend-template flattener: keeps only the first entry of a message dict."""
    if isinstance(obj, str):
        return [obj]
    entries = _message_entries(obj) if isinstance(obj, dict) else None
    if entries:
        return entries[:1]
    return [str(obj)]


def flatten_keyed(obj: Any) -> list[str]:
    """cafm-backend / user-management flattener: one `key - value` message per field."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        entries = _message_entries(obj)
        if entries:
            return entries
        return [f"{key} - {value}" for key, value in obj.items()]
    return [str(obj)]


def first_error(value: Any) -> str:
    """The first message for one field of a DRF error dict; a bare string is returned whole."""
    if isinstance(value, (list, tuple)):
        return first_error(value[0]) if value else ""
    if isinstance(value, dict):
        return ", ".join(f"{key}: {first_error(inner)}" for key, inner in value.items())
    return str(value)


def flatten_first_error(obj: Any) -> list[str]:
    """tag-service flattener: keyed like flatten_keyed but nesting-aware via first_error."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        entries = _message_entries(obj)
        if entries:
            return entries[:1]
        return [f"{key} - {first_error(value)}" for key, value in obj.items()]
    return [str(obj)]


def _iter_messages_from_mapping(mapping: dict[str, Any]) -> Iterator[str]:
    for key, value in mapping.items():
        if isinstance(value, (list, tuple, set)):
            yield from (f"{key}: {item}" for item in value)
        elif isinstance(value, dict):
            yield from _iter_messages_from_mapping(value)
        else:
            yield f"{key}: {value}"


def flatten_nested(obj: Any) -> list[str]:
    """data-service flattener: recurses into nested dicts and expands sequence values."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return list(_iter_messages_from_mapping(obj))
    return [str(obj)]


def flatten_sequence(obj: Any) -> list[str]:
    """static-service flattener: recurses through lists/tuples/sets of error payloads."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        entries = _message_entries(obj)
        if entries:
            return entries[:1]
        return [str(obj)]
    if isinstance(obj, (list, tuple, set)):
        messages: list[str] = []
        for item in obj:
            messages.extend(flatten_sequence(item))
        return messages
    return [str(obj)]


FLATTENERS: Final[dict[str, Flattener]] = {
    "template": flatten_template,
    "keyed": flatten_keyed,
    "first_error": flatten_first_error,
    "nested": flatten_nested,
    "sequence": flatten_sequence,
}


def resolve_flattener(name: str | None = None) -> Flattener:
    """Resolve the flattener by name, defaulting to NETIX_ERRORS_FLATTENER read at call time."""
    if name is None:
        name = getattr(settings, "NETIX_ERRORS_FLATTENER", DEFAULT_FLATTENER)
    try:
        return FLATTENERS[name]
    except KeyError:
        raise ImproperlyConfigured(
            f"Unknown NETIX_ERRORS_FLATTENER {name!r}; expected one of {sorted(FLATTENERS)}"
        ) from None


def build_messages(exc: Exception, *, flattener: Flattener | str | None = None) -> list[str]:
    """Normalise any exception to a list of human-readable messages, never str()-ing the raw exception."""
    if isinstance(exc, ValidationError):
        flatten = flattener if callable(flattener) else resolve_flattener(flattener)
        return [message for arg in exc.args for message in flatten(arg)]
    if isinstance(exc, Http404):
        return [HTTP404_MESSAGE]
    detail: Any = getattr(exc, "detail", None)
    if detail is None:
        detail = getattr(exc, "message", None)
    if detail is None:
        detail = str(exc)
    if isinstance(detail, (list, tuple, set)):
        return [str(item) for item in detail]
    if isinstance(detail, dict):
        return [f"{key}: {value}" for key, value in detail.items()]
    return [str(detail)]


def render_messages(messages: list[str], *, stringified: bool | None = None, as_list: bool = True) -> str | list[str]:
    """The single place the wire shape of `messages` is decided."""
    if not as_list:
        return messages[0] if messages else ""
    if stringified is None:
        stringified = bool(getattr(settings, "NETIX_ERRORS_STRINGIFIED", True))
    return str(messages) if stringified else list(messages)


def _as_list_for(exc: Exception) -> bool:
    if isinstance(exc, ValidationError):
        return True
    if isinstance(exc, Http404):
        return bool(getattr(settings, "NETIX_ERRORS_HTTP404_AS_LIST", True))
    return bool(getattr(settings, "NETIX_ERRORS_NON_VALIDATION_AS_LIST", True))


def custom_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    """DRF EXCEPTION_HANDLER: delegates to DRF first, then rewrites the body to the NETIX envelope."""
    response = exception_handler(exc, context)

    if response is None:
        db_unavailable = getattr(settings, "NETIX_ERRORS_DB_UNAVAILABLE_503", False)
        if db_unavailable and isinstance(exc, (DatabaseError, OperationalError)):
            logger.exception("Unhandled database error during request")
            return Response({"status_code": 503, "messages": DB_UNAVAILABLE_MESSAGE}, status=503)
        return None

    response.data = {
        "status_code": response.status_code,
        "messages": render_messages(build_messages(exc), as_list=_as_list_for(exc)),
    }
    return response
