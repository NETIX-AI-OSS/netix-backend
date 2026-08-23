"""static-service upload/download helpers with the 200-vs-201 dedup contract and a raising failure mode."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from http import HTTPStatus
from typing import Any

import httpx

from netix_backend.env import env_bool, env_float, env_str, service_authorization
from netix_backend.http.retry import (
    RETRYABLE_EXC,
    RetryTransport,
    default_backoff,
    default_retries,
    is_retryable_status,
    jittered_backoff,
)

logger = logging.getLogger(__name__)

UPLOAD_PATH = "/api/static/"
DEFAULT_TIMEOUT_SECONDS = 30.0

# 200 is the global-dedup hit static-service returns for identical content; only 201 means a fresh object.
ACCEPTED_UPLOAD_STATUS = frozenset({HTTPStatus.OK, HTTPStatus.CREATED})

StaticFilePayload = dict[str, Any]


class StaticServiceError(Exception):
    """Non-retryable static-service failure."""


class StaticFileExpired(StaticServiceError):
    """The static-service file no longer exists (404), typically past its retention date."""


def build_static_client(
    *,
    timeout: float | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """httpx client for static-service, authenticated with this pod's own service credential."""
    seconds = env_float("STATIC_SVC_TIMEOUT", DEFAULT_TIMEOUT_SECONDS) if timeout is None else timeout
    if transport is None:
        # retries=0: the helpers own the retry loop, and a retrying transport under it would square the attempts.
        transport = RetryTransport(retries=0, verify=env_bool("STATIC_SVC_VERIFY_SSL"))
    return httpx.Client(
        base_url=env_str("STATIC_SVC_URL", required=True),
        headers={"Authorization": service_authorization("STATIC_SVC_AUTH")},
        timeout=httpx.Timeout(seconds),
        transport=transport,
    )


@contextmanager
def _client_context(
    client: httpx.Client | None,
    timeout: float | None,
    transport: httpx.BaseTransport | None,
) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    created = build_static_client(timeout=timeout, transport=transport)
    try:
        yield created
    finally:
        created.close()


def _iso(value: datetime | date | str) -> str:
    return value if isinstance(value, str) else value.isoformat()


def _attempts(retries: int | None) -> int:
    return (default_retries() if retries is None else retries) + 1


def _attempt(call: Callable[[], httpx.Response], what: str) -> tuple[httpx.Response | None, StaticServiceError | None]:
    """Run one request, turning a transient transport failure into the error type callers are told to catch."""
    try:
        return call(), None
    except RETRYABLE_EXC as exc:
        return None, StaticServiceError(f"static-service {what} could not reach the service: {exc!r}")


def _upload_payload(response: httpx.Response) -> StaticFilePayload:
    payload: StaticFilePayload = response.json()
    if not payload.get("link"):
        raise StaticServiceError(f"static-service upload response carries no 'link': {payload!r}")
    # `file` is write-only and omitted from responses, but the generated File model requires it.
    payload.setdefault("file", payload["link"])
    return payload


def static_upload(
    *,
    name: str,
    content: bytes,
    mime: str,
    retention_date: datetime | date | str,
    description: str | None = None,
    timeout: float | None = None,
    retries: int | None = None,
    client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
) -> StaticFilePayload:
    """Upload to ``/api/static/`` and return the file payload; *retention_date* is what the purge job later honours."""
    data = {"name": name, "mime": mime, "retention_date": _iso(retention_date)}
    if description is not None:
        data["description"] = description
    files = {"file": (name, content, mime)}
    attempts = _attempts(retries)
    backoff = default_backoff()
    last_error: StaticServiceError | None = None
    with _client_context(client, timeout, transport) as http_client:
        for attempt in range(attempts):
            response, last_error = _attempt(lambda: http_client.post(UPLOAD_PATH, data=data, files=files), "upload")
            if response is not None:
                if response.status_code in ACCEPTED_UPLOAD_STATUS:
                    return _upload_payload(response)
                if not is_retryable_status(response.status_code):
                    raise StaticServiceError(
                        f"static-service upload failed: status={response.status_code} body={response.text[:200]!r}"
                    )
                last_error = StaticServiceError(
                    f"static-service upload returned {response.status_code} (attempt {attempt + 1}/{attempts})"
                )
            if attempt + 1 < attempts:
                time.sleep(jittered_backoff(backoff, attempt))
    raise last_error or StaticServiceError("static-service upload made no attempts; retries must be >= 0")


def link_or_empty(**kwargs: Any) -> str:
    """Upload and return only the link, "" on failure; prefer :func:`static_upload` — silence hides dedup breakage."""
    try:
        return str(static_upload(**kwargs)["link"])
    except StaticServiceError as exc:
        logger.error("static-service upload failed, returning an empty link: %s", exc)
        return ""


def _fetch(
    link: str,
    *,
    timeout: float | None,
    retries: int | None,
    client: httpx.Client | None,
    transport: httpx.BaseTransport | None,
) -> httpx.Response:
    attempts = _attempts(retries)
    backoff = default_backoff()
    last_error: StaticServiceError | None = None
    with _client_context(client, timeout, transport) as http_client:
        for attempt in range(attempts):
            response, last_error = _attempt(lambda: http_client.get(link), "download")
            if response is not None:
                if response.status_code == HTTPStatus.OK:
                    return response
                if response.status_code == HTTPStatus.NOT_FOUND:
                    raise StaticFileExpired(f"static-service file not found at {link!r} (likely past retention)")
                if not is_retryable_status(response.status_code):
                    raise StaticServiceError(
                        f"static-service download failed: status={response.status_code} body={response.text[:200]!r}"
                    )
                last_error = StaticServiceError(
                    f"static-service download returned {response.status_code} (attempt {attempt + 1}/{attempts})"
                )
            if attempt + 1 < attempts:
                time.sleep(jittered_backoff(backoff, attempt))
    raise last_error or StaticServiceError("static-service download made no attempts; retries must be >= 0")


def static_fetch(
    link: str,
    *,
    timeout: float | None = None,
    retries: int | None = None,
    client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Download the bytes at *link*; a 404 raises :class:`StaticFileExpired` rather than being retried like a 5xx."""
    return _fetch(link, timeout=timeout, retries=retries, client=client, transport=transport).content


def static_fetch_text(
    link: str,
    *,
    timeout: float | None = None,
    retries: int | None = None,
    client: httpx.Client | None = None,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Download the body at *link* decoded with the response charset."""
    return _fetch(link, timeout=timeout, retries=retries, client=client, transport=transport).text


__all__ = (
    "ACCEPTED_UPLOAD_STATUS",
    "UPLOAD_PATH",
    "StaticFileExpired",
    "StaticFilePayload",
    "StaticServiceError",
    "build_static_client",
    "link_or_empty",
    "static_fetch",
    "static_fetch_text",
    "static_upload",
)
