"""Re-export of the self-registration client, for WSGI-only services that never import an ASGI app."""

from netix_backend.asgi.registration import (
    ENABLED_ENV,
    REGISTRATION_FIELDS,
    REGISTRATION_TIMEOUT_SECONDS,
    RETRY_DELAYS,
    THREAD_NAME,
    TOKEN_ENV,
    TOKEN_HEADER,
    URL_ENV,
    ServiceRegistrationASGI,
    ServiceRegistrationWSGI,
    register,
    registration_enabled,
    registration_payload,
    registration_started,
    reset_registration_state,
    trigger_service_registration,
)

__all__ = (
    "ENABLED_ENV",
    "REGISTRATION_FIELDS",
    "REGISTRATION_TIMEOUT_SECONDS",
    "RETRY_DELAYS",
    "THREAD_NAME",
    "TOKEN_ENV",
    "TOKEN_HEADER",
    "URL_ENV",
    "ServiceRegistrationASGI",
    "ServiceRegistrationWSGI",
    "register",
    "registration_enabled",
    "registration_payload",
    "registration_started",
    "reset_registration_state",
    "trigger_service_registration",
)
