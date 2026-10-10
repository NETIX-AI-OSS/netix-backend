"""Peer URLs and frontend origins from update-service's live registry, cached per process, env-backed."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from netix_backend.asgi.registration import TOKEN_ENV, TOKEN_HEADER, URL_ENV
from netix_backend.env import env_str

logger = logging.getLogger(__name__)

ENVIRONMENT_ENV = "SERVICE_REGISTRATION_ENVIRONMENT"
CACHE_SECONDS = 60.0
TIMEOUT_SECONDS = 2.0


@dataclass
class _Snapshot:
    expires: float | None = None
    urls: dict[str, str] = field(default_factory=dict)
    origins: frozenset[str] = frozenset()


_lock = threading.Lock()
_snapshot = _Snapshot()


def discovery_url() -> str | None:
    """The registration route's sibling, so no service needs a second update-service URL in its ConfigMap."""
    registration_url = env_str(URL_ENV)
    return urljoin(registration_url, "../discovery/") if registration_url else None


def _fetch() -> tuple[dict[str, str], frozenset[str]] | None:
    url, token, environment = discovery_url(), env_str(TOKEN_ENV), env_str(ENVIRONMENT_ENV)
    if not (url and token and environment):
        return None
    request = Request(f"{url}?{urlencode({'environment': environment})}", headers={TOKEN_HEADER: token})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            services = json.load(response)["services"]
        urls = {entry["name"]: entry["internal_base_url"] for entry in services if entry.get("internal_base_url")}
        origins = frozenset(
            entry["base_url"].rstrip("/")
            for entry in services
            if entry.get("kind") == "frontend" and entry.get("base_url")
        )
    except Exception:
        logger.warning("Service discovery failed; keeping the last known URLs", exc_info=True)
        return None
    return urls, origins


def _refresh() -> None:
    if (fetched := _fetch()) is not None:
        _snapshot.urls, _snapshot.origins = fetched


def _current() -> _Snapshot:
    with _lock:
        now = time.monotonic()
        if _snapshot.expires is None:
            _snapshot.expires = now + CACHE_SECONDS
            _refresh()
        elif now >= _snapshot.expires:
            _snapshot.expires = now + CACHE_SECONDS
            # Only the first lookup blocks: CORS checks call this from the event loop on every cross-origin request.
            threading.Thread(target=_refresh, name="service-discovery", daemon=True).start()
    return _snapshot


def discovered_urls() -> dict[str, str]:
    """Registry name -> internal base URL, refreshed in the background every CACHE_SECONDS, kept through outages."""
    return _current().urls


def frontend_origins() -> frozenset[str]:
    """Public origins of the frontends live in the registry, for CORS and CSRF trust."""
    return _current().origins


def service_url(name: str, fallback_env: str | None = None) -> str | None:
    """A peer's internal base URL: its live (or overridden) registry entry, else the *fallback_env* variable."""
    return discovered_urls().get(name) or (env_str(fallback_env) if fallback_env else None)


def reset_discovery_cache() -> None:
    """Forget the cached registry; only tests need this, to isolate environment changes."""
    with _lock:
        _snapshot.expires, _snapshot.urls, _snapshot.origins = None, {}, frozenset()


__all__ = (
    "CACHE_SECONDS",
    "ENVIRONMENT_ENV",
    "TIMEOUT_SECONDS",
    "discovered_urls",
    "discovery_url",
    "frontend_origins",
    "reset_discovery_cache",
    "service_url",
)
