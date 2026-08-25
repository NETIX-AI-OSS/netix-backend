"""ASGI/WSGI entrypoints: the request ceiling, the lifespan handshake, and service self-registration."""

# Importable without Django configured; every settings read is lazy.
# netix_backend.asgi.testing stays out of these exports because it imports pytest.

from netix_backend.asgi.lifespan import LifespanWrapper, serve_lifespan, wrap
from netix_backend.asgi.registration import (
    RETRY_DELAYS,
    ServiceRegistrationASGI,
    ServiceRegistrationWSGI,
    register,
    registration_enabled,
    registration_payload,
    registration_started,
    reset_registration_state,
    trigger_service_registration,
)
from netix_backend.asgi.timeout import (
    MAX_ABANDONED_REQUESTS,
    NEVER_SHED,
    OVERLOADED_MESSAGE,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_AFTER_SECONDS,
    SAFE_METHODS,
    TIMEOUT_MESSAGE,
    ErrorRenderer,
    RequestTimeoutMiddleware,
    cors_headers,
    django_setting,
    error_messages,
    text_messages,
)
from netix_backend.asgi.types import ASGIApp, Message, Receive, Scope, Send, StartResponse, WSGIApp, WSGIEnvironment

__all__ = (
    "MAX_ABANDONED_REQUESTS",
    "NEVER_SHED",
    "OVERLOADED_MESSAGE",
    "REQUEST_TIMEOUT_SECONDS",
    "RETRY_AFTER_SECONDS",
    "RETRY_DELAYS",
    "SAFE_METHODS",
    "TIMEOUT_MESSAGE",
    "ASGIApp",
    "ErrorRenderer",
    "LifespanWrapper",
    "Message",
    "Receive",
    "RequestTimeoutMiddleware",
    "Scope",
    "Send",
    "ServiceRegistrationASGI",
    "ServiceRegistrationWSGI",
    "StartResponse",
    "WSGIApp",
    "WSGIEnvironment",
    "cors_headers",
    "django_setting",
    "error_messages",
    "register",
    "registration_enabled",
    "registration_payload",
    "registration_started",
    "reset_registration_state",
    "serve_lifespan",
    "text_messages",
    "trigger_service_registration",
    "wrap",
)
