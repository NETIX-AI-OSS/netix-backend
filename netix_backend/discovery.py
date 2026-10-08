"""Peer base URLs from update-service's live registry, cached per process and backed by the legacy env variables."""

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
    expires: float = 0.0
    urls: dict[str, str] = field(default_factory=dict)


_lock = threading.Lock()
_snapshot = _Snapshot()


def discovery_url() -> str | None:
    """The registration route's sibling, so no service needs a second update-service URL in its ConfigMap."""
    registration_url = env_str(URL_ENV)
    return urljoin(registration_url, "../discovery/") if registration_url else None


def _fetch() -> dict[str, str] | None:
    url, token, environment = discovery_url(), env_str(TOKEN_ENV), env_str(ENVIRONMENT_ENV)
    if not (url and token and environment):
        return None
    request = Request(f"{url}?{urlencode({'environment': environment})}", headers={TOKEN_HEADER: token})
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            services = json.load(response)["services"]
        return {entry["name"]: entry["internal_base_url"] for entry in services if entry.get("internal_base_url")}
    except Exception:
        logger.warning("Service discovery failed; keeping the last known URLs", exc_info=True)
        return None


def discovered_urls() -> dict[str, str]:
    """Registry name -> internal base URL, refetched at most every CACHE_SECONDS and kept through a failed fetch."""
    with _lock:
        if time.monotonic() >= _snapshot.expires:
            _snapshot.expires = time.monotonic() + CACHE_SECONDS
            urls = _fetch()
            if urls is not None:
                _snapshot.urls = urls
        return _snapshot.urls


def service_url(name: str, fallback_env: str | None = None) -> str | None:
    """A peer's internal base URL: its live (or overridden) registry entry, else the *fallback_env* variable."""
    return discovered_urls().get(name) or (env_str(fallback_env) if fallback_env else None)


def reset_discovery_cache() -> None:
    """Forget the cached registry; only tests need this, to isolate environment changes."""
    with _lock:
        _snapshot.expires, _snapshot.urls = 0.0, {}


__all__ = (
    "CACHE_SECONDS",
    "ENVIRONMENT_ENV",
    "TIMEOUT_SECONDS",
    "discovered_urls",
    "discovery_url",
    "reset_discovery_cache",
    "service_url",
)
