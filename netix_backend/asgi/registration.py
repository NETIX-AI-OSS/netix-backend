"""Non-blocking registration of this service with update-service; stdlib only, so it loads before django.setup()."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Final
from urllib.request import Request, urlopen

from netix_backend.asgi.types import ASGIApp, Receive, Scope, Send, StartResponse, WSGIApp, WSGIEnvironment
from netix_backend.env import env_bool, env_float, env_str

logger = logging.getLogger(__name__)

ENABLED_ENV = "SERVICE_REGISTRATION_ENABLED"
URL_ENV = "UPDATE_SERVICE_REGISTRATION_URL"
TOKEN_ENV = "SERVICE_REGISTRATION_TOKEN"
TOKEN_HEADER = "X-Service-Registration-Token"
THREAD_NAME = "service-registration"

# Eight attempts over ~121s, sized to outlast an update-service that is itself still starting.
RETRY_DELAYS: Final[tuple[float, ...]] = (0, 1, 2, 4, 8, 16, 30, 60)

REGISTRATION_TIMEOUT_SECONDS = 5

# Re-PUT cadence that keeps the registration inside update-service's liveness TTL; 0 disables the heartbeat.
HEARTBEAT_ENV = "SERVICE_REGISTRATION_HEARTBEAT_SECONDS"
HEARTBEAT_SECONDS = 60.0

# The registry payload is entirely env-driven; no service contributes code to its own identity.
REGISTRATION_FIELDS: Final[Mapping[str, str]] = {
    "name": "SERVICE_NAME",
    "display_name": "SERVICE_DISPLAY_NAME",
    "description": "SERVICE_DESCRIPTION",
    "version": "APP_VERSION",
    "environment": "SERVICE_REGISTRATION_ENVIRONMENT",
    "base_url": "SERVICE_PUBLIC_BASE_URL",
    "internal_base_url": "SERVICE_INTERNAL_BASE_URL",
}

_registration_lock = threading.Lock()
_registration_state: Final[dict[str, bool]] = {"started": False}
_heartbeat_stop: Final[dict[str, threading.Event]] = {"event": threading.Event()}
# Bound at import: consumer tests swap threading.Thread for an inline fake, under which this loop would never return.
_HeartbeatThread = threading.Thread


def registration_enabled() -> bool:
    """Whether SERVICE_REGISTRATION_ENABLED holds one of the ConfigMap spellings of true."""
    return env_bool(ENABLED_ENV)


def registration_started() -> bool:
    """Whether this process has already latched its one registration attempt."""
    return _registration_state["started"]


def reset_registration_state() -> None:
    """Release the once-per-process latch; the hook consumer tests need now that the latch is shared."""
    with _registration_lock:
        _registration_state["started"] = False
        _heartbeat_stop["event"].set()
        _heartbeat_stop["event"] = threading.Event()


def registration_payload() -> tuple[str, str, dict[str, str]] | None:
    """The (url, token, payload) triple, or None when registration is disabled or under-configured."""
    if not registration_enabled():
        return None

    values = {field: env_str(env_name, "") for field, env_name in REGISTRATION_FIELDS.items()}
    registration_url = env_str(URL_ENV, "")
    token = env_str(TOKEN_ENV, "")
    missing = [env_name for field, env_name in REGISTRATION_FIELDS.items() if not values[field]]
    if not registration_url:
        missing.append(URL_ENV)
    if not token:
        missing.append(TOKEN_ENV)
    if missing:
        logger.error("Service registration skipped; missing environment variables: %s", ", ".join(missing))
        return None
    return registration_url, token, values


def _put(configuration: tuple[str, str, dict[str, str]], timeout: float) -> None:
    registration_url, token, payload = configuration
    request = Request(
        registration_url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json", TOKEN_HEADER: token},
        method="PUT",
    )
    # The URL is operator-configured, and stdlib urllib keeps httpx out of the asgi/wsgi import path.
    with urlopen(request, timeout=timeout) as response:
        if not 200 <= response.status < 300:
            raise OSError(f"update-service returned HTTP {response.status}")


def register(retry_delays: Sequence[float] = RETRY_DELAYS, *, timeout: float = REGISTRATION_TIMEOUT_SECONDS) -> bool:
    """PUT this service's identity to update-service, retrying along *retry_delays*; True once it is accepted."""
    configuration = registration_payload()
    if configuration is None:
        return False

    for attempt, delay in enumerate(retry_delays, start=1):
        if delay:
            time.sleep(delay)
        try:
            _put(configuration, timeout)
        except Exception:
            # Every failure is retryable here, including a non-2xx.
            if attempt == len(retry_delays):
                logger.exception("Service registration failed after %d attempts", attempt)
            else:
                logger.warning("Service registration attempt %d failed; retrying", attempt, exc_info=True)
        else:
            logger.info("Registered service %s with update-service", configuration[2]["name"])
            return True
    return False


def _heartbeat(seconds: float, stop: threading.Event) -> None:
    while not stop.wait(seconds):
        if (configuration := registration_payload()) is None:
            return
        try:
            _put(configuration, REGISTRATION_TIMEOUT_SECONDS)
        except Exception:
            # Warning, not error: an update-service outage must not page once a minute from every pod.
            logger.warning("Service registration heartbeat failed", exc_info=True)


def start_heartbeat(interval: float | None = None) -> bool:
    """Re-PUT the registration every *interval* seconds until the latch is reset, so update-service sees it live."""
    seconds = env_float(HEARTBEAT_ENV, HEARTBEAT_SECONDS) if interval is None else interval
    if seconds <= 0:
        return False
    _HeartbeatThread(
        target=_heartbeat, args=(seconds, _heartbeat_stop["event"]), name=f"{THREAD_NAME}-heartbeat", daemon=True
    ).start()
    return True


def _register_and_heartbeat(retry_delays: Sequence[float] = RETRY_DELAYS) -> None:
    if registration_payload() is not None:
        register(retry_delays)
        start_heartbeat()


def trigger_service_registration(retry_delays: Sequence[float] = RETRY_DELAYS) -> bool:
    """Start registration plus heartbeat once per process without delaying the request; True if this call started it."""
    if not registration_enabled():
        return False
    with _registration_lock:
        if _registration_state["started"]:
            return False
        _registration_state["started"] = True
    threading.Thread(target=_register_and_heartbeat, args=(retry_delays,), name=THREAD_NAME, daemon=True).start()
    return True


# Trigger registration on the process's first HTTP request.
class ServiceRegistrationASGI:
    def __init__(self, application: ASGIApp) -> None:
        self.application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            trigger_service_registration()
        await self.application(scope, receive, send)


# Trigger registration on the process's first WSGI request.
class ServiceRegistrationWSGI:
    def __init__(self, application: WSGIApp) -> None:
        self.application = application

    def __call__(self, environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        trigger_service_registration()
        return self.application(environ, start_response)


__all__ = (
    "ENABLED_ENV",
    "HEARTBEAT_ENV",
    "HEARTBEAT_SECONDS",
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
    "start_heartbeat",
    "trigger_service_registration",
)
