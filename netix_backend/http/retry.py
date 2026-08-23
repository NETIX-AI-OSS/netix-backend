"""Shared httpx retry policy: transports, one-shot helpers and the fleet-wide defaults."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import Any

import httpx

from netix_backend.env import env_float, env_int

logger = logging.getLogger(__name__)

RETRIES_ENV = "NETIX_HTTP_RETRIES"
BACKOFF_ENV = "NETIX_HTTP_RETRY_BACKOFF"

# retries=2 => three total attempts (initial + 2 retries).
DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_FACTOR = 0.5
MAX_RETRY_AFTER_SECONDS = 30.0

RETRYABLE_STATUS = frozenset({HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS})

# Status-based retries only — connection/timeout errors may retry any method.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# NetworkError + TimeoutException subsume every connect/read/write/pool leaf class; never enumerate leaves.
RETRYABLE_EXC = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)

SyncSend = Callable[[httpx.Request], httpx.Response]
AsyncSend = Callable[[httpx.Request], Awaitable[httpx.Response]]

_bound_settings: dict[str, float] = {}


def bind_django_settings(*, retries_setting: str | None = None, backoff_setting: str | None = None) -> None:
    """Alias a repo's legacy retry settings onto the shared knobs; env still wins, so call once at startup."""
    from django.conf import settings  # local import: the transports must also load in migrations and plain scripts

    if retries_setting is not None:
        retries = getattr(settings, retries_setting, None)
        if retries is not None:
            _bound_settings["retries"] = float(retries)
    if backoff_setting is not None:
        backoff = getattr(settings, backoff_setting, None)
        if backoff is not None:
            _bound_settings["backoff"] = float(backoff)


def unbind_django_settings() -> None:
    """Drop every alias registered by :func:`bind_django_settings`; only tests need this."""
    _bound_settings.clear()


def default_retries() -> int:
    """Retry count from ``NETIX_HTTP_RETRIES``, then any bound Django setting, else 2."""
    return env_int(RETRIES_ENV, int(_bound_settings.get("retries", DEFAULT_RETRIES)))


def default_backoff() -> float:
    """Backoff factor from ``NETIX_HTTP_RETRY_BACKOFF``, then any bound Django setting, else 0.5."""
    return env_float(BACKOFF_ENV, _bound_settings.get("backoff", DEFAULT_BACKOFF_FACTOR))


def is_idempotent_method(method: str) -> bool:
    """True for methods safe to auto-retry on an ambiguous HTTP status."""
    return method.upper() in IDEMPOTENT_METHODS


def is_retryable_status(status_code: int) -> bool:
    """True for 408, 429 or any 5xx."""
    return status_code in RETRYABLE_STATUS or status_code >= HTTPStatus.INTERNAL_SERVER_ERROR


def should_retry_status(method: str, status_code: int, *, retry_non_idempotent: bool = False) -> bool:
    """Status retries are gated on idempotent methods unless the caller opts in explicitly."""
    if not is_retryable_status(status_code):
        return False
    return retry_non_idempotent or is_idempotent_method(method)


def jittered_backoff(backoff_factor: float, attempt: int) -> float:
    """Equal-jitter exponential backoff ``cap/2 + U(0, cap/2)``; avoids a thundering herd on service restart."""
    cap: float = backoff_factor * (2**attempt)
    return cap / 2 + random.uniform(0, cap / 2)


def parse_retry_after_seconds(
    response: httpx.Response,
    *,
    max_retry_after: float = MAX_RETRY_AFTER_SECONDS,
) -> float | None:
    """Parse ``Retry-After`` in either the seconds or the HTTP-date form, clamped to *max_retry_after*."""
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(max(float(value), 0.0), max_retry_after)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        delay = when.timestamp() - time.time()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    return min(max(delay, 0.0), max_retry_after)


def _log_target(url: httpx.URL) -> str:
    """Return ``origin + path`` for logging, dropping the query string so tokens never reach the log."""
    return str(url.copy_with(query=None, fragment=None))


class _RetryPolicy:
    """Retry state and decisions shared by the sync and async transports."""

    retries: int
    backoff_factor: float
    retry_non_idempotent: bool
    max_retry_after: float

    def _init_policy(
        self,
        retries: int | None,
        backoff_factor: float | None,
        retry_non_idempotent: bool,
        max_retry_after: float,
    ) -> None:
        # Never `or`: retries=0 must issue exactly one attempt instead of silently restoring the default.
        self.retries = default_retries() if retries is None else retries
        self.backoff_factor = default_backoff() if backoff_factor is None else backoff_factor
        self.retry_non_idempotent = retry_non_idempotent
        self.max_retry_after = max_retry_after

    def _should_retry(self, request: httpx.Request, response: httpx.Response) -> bool:
        return should_retry_status(
            request.method,
            response.status_code,
            retry_non_idempotent=self.retry_non_idempotent,
        )

    def _status_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = parse_retry_after_seconds(response, max_retry_after=self.max_retry_after)
        if retry_after is not None:
            return retry_after
        return jittered_backoff(self.backoff_factor, attempt)

    def _log_status_retry(self, request: httpx.Request, response: httpx.Response, delay: float, attempt: int) -> None:
        logger.warning(
            "Request to %s returned status %s; retrying in %.2fs (%s/%s)",
            _log_target(request.url),
            response.status_code,
            delay,
            attempt + 1,
            self.retries,
        )

    def _log_exception_retry(self, request: httpx.Request, exc: Exception, delay: float, attempt: int) -> None:
        logger.warning(
            "Request to %s failed with %s; retrying in %.2fs (%s/%s)",
            _log_target(request.url),
            type(exc).__name__,
            delay,
            attempt + 1,
            self.retries,
        )

    def _log_status_exhausted(self, request: httpx.Request, response: httpx.Response, attempts: int) -> None:
        logger.error(
            "Request to %s still returned status %s after %s attempt(s)",
            _log_target(request.url),
            response.status_code,
            attempts,
        )

    def _log_exception_exhausted(self, request: httpx.Request, exc: Exception, attempts: int) -> None:
        logger.error(
            "Request to %s failed with %s after %s attempt(s)",
            _log_target(request.url),
            type(exc).__name__,
            attempts,
        )

    def _send_with_retries(self, request: httpx.Request, send: SyncSend) -> httpx.Response:
        """Drive *send* until it succeeds or the retry budget is spent; shared by the transport and the wrapper."""
        attempt = 0
        while True:
            try:
                response = send(request)
            except RETRYABLE_EXC as exc:
                if attempt >= self.retries:
                    self._log_exception_exhausted(request, exc, attempt + 1)
                    raise
                delay = jittered_backoff(self.backoff_factor, attempt)
                self._log_exception_retry(request, exc, delay, attempt)
            else:
                if not self._should_retry(request, response):
                    return response
                if attempt >= self.retries:
                    self._log_status_exhausted(request, response, attempt + 1)
                    return response
                delay = self._status_delay(response, attempt)
                self._log_status_retry(request, response, delay, attempt)
                # Retry-After is already read, so release the pooled connection before sleeping on it.
                response.close()
            time.sleep(delay)
            attempt += 1

    async def _asend_with_retries(self, request: httpx.Request, send: AsyncSend) -> httpx.Response:
        """Async twin of :meth:`_send_with_retries`."""
        attempt = 0
        while True:
            try:
                response = await send(request)
            except RETRYABLE_EXC as exc:
                if attempt >= self.retries:
                    self._log_exception_exhausted(request, exc, attempt + 1)
                    raise
                delay = jittered_backoff(self.backoff_factor, attempt)
                self._log_exception_retry(request, exc, delay, attempt)
            else:
                if not self._should_retry(request, response):
                    return response
                if attempt >= self.retries:
                    self._log_status_exhausted(request, response, attempt + 1)
                    return response
                delay = self._status_delay(response, attempt)
                self._log_status_retry(request, response, delay, attempt)
                # Retry-After is already read, so release the pooled connection before sleeping on it.
                await response.aclose()
            await asyncio.sleep(delay)
            attempt += 1


class RetryTransport(_RetryPolicy, httpx.HTTPTransport):
    """httpx transport retrying transient network errors and retryable statuses."""

    def __init__(
        self,
        retries: int | None = None,
        backoff_factor: float | None = None,
        *,
        retry_non_idempotent: bool = False,
        max_retry_after: float = MAX_RETRY_AFTER_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._init_policy(retries, backoff_factor, retry_non_idempotent, max_retry_after)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Issue the request, retrying transient failures until the retry budget is spent."""
        return self._send_with_retries(request, super().handle_request)


class AsyncRetryTransport(_RetryPolicy, httpx.AsyncHTTPTransport):
    """Async twin of :class:`RetryTransport`."""

    def __init__(
        self,
        retries: int | None = None,
        backoff_factor: float | None = None,
        *,
        retry_non_idempotent: bool = False,
        max_retry_after: float = MAX_RETRY_AFTER_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._init_policy(retries, backoff_factor, retry_non_idempotent, max_retry_after)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Issue the request, retrying transient failures until the retry budget is spent."""
        return await self._asend_with_retries(request, super().handle_async_request)


class RetryTransportWrapper(_RetryPolicy, httpx.BaseTransport):
    """The shared retry policy applied to an existing transport, for callers that inject their own."""

    def __init__(
        self,
        transport: httpx.BaseTransport,
        retries: int | None = None,
        backoff_factor: float | None = None,
        *,
        retry_non_idempotent: bool = False,
        max_retry_after: float = MAX_RETRY_AFTER_SECONDS,
    ) -> None:
        self.transport = transport
        self._init_policy(retries, backoff_factor, retry_non_idempotent, max_retry_after)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Issue the request through the wrapped transport under the shared retry policy."""
        return self._send_with_retries(request, self.transport.handle_request)

    def close(self) -> None:
        """Close the wrapped transport, so ``with httpx.Client(...)`` still releases its pool."""
        self.transport.close()


def get_retry_transport(**kwargs: Any) -> RetryTransport:
    """Build a sync retry transport for ``httpx_args={"transport": ...}``."""
    return RetryTransport(**kwargs)


def get_async_retry_transport(**kwargs: Any) -> AsyncRetryTransport:
    """Build an async retry transport for ``set_async_httpx_client``."""
    return AsyncRetryTransport(**kwargs)


def interservice_httpx_args(**extra: Any) -> dict[str, Any]:
    """``httpx_args`` for a generated OpenAPI client, carrying the shared retry transport."""
    return {"transport": get_retry_transport(), **extra}


def _cap(value: float | None, remaining: float) -> float:
    return remaining if value is None else min(value, remaining)


def _attempt_timeout(timeout: float | httpx.Timeout, remaining: float) -> httpx.Timeout:
    """Per-attempt timeout, never allowed to outlive the remaining deadline budget."""
    base = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout)
    return httpx.Timeout(
        connect=_cap(base.connect, remaining),
        read=_cap(base.read, remaining),
        write=_cap(base.write, remaining),
        pool=_cap(base.pool, remaining),
    )


def _request_within_deadline(
    method: str,
    url: str,
    *,
    timeout: float | httpx.Timeout,
    deadline: float,
    retries: int,
    backoff_factor: float,
    retry_non_idempotent: bool,
    transport: httpx.BaseTransport | None,
    **kwargs: Any,
) -> httpx.Response:
    """Retry under a whole-loop deadline; per-attempt timeouts stop a hung connection blocking past it."""
    end = time.monotonic() + deadline
    last_response: httpx.Response | None = None
    last_exc: Exception | None = None
    with httpx.Client(transport=transport) as client:
        for attempt in range(retries + 1):
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            try:
                response = client.request(method, url, timeout=_attempt_timeout(timeout, remaining), **kwargs)
            except RETRYABLE_EXC as exc:
                last_exc = exc
            else:
                if not should_retry_status(method, response.status_code, retry_non_idempotent=retry_non_idempotent):
                    return response
                last_response = response
            if attempt >= retries:
                break
            budget = end - time.monotonic()
            if budget <= 0:
                break
            time.sleep(min(jittered_backoff(backoff_factor, attempt), budget))
    if last_response is not None:
        return last_response
    if last_exc is not None:
        raise last_exc
    raise httpx.TimeoutException(f"{method} {url} exceeded its {deadline}s deadline before any attempt completed")


def request_with_retry(
    method: str,
    url: str,
    *,
    timeout: float | httpx.Timeout,
    retries: int | None = None,
    backoff_factor: float | None = None,
    retry_non_idempotent: bool = False,
    deadline: float | None = None,
    transport: httpx.BaseTransport | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """One-shot request with the shared retry policy; *deadline* caps the whole loop, not just one attempt.

    Exhausted status retries return the last response rather than raising — check ``response.status_code``.
    """
    attempts = default_retries() if retries is None else retries
    factor = default_backoff() if backoff_factor is None else backoff_factor
    if deadline is not None:
        return _request_within_deadline(
            method,
            url,
            timeout=timeout,
            deadline=deadline,
            retries=attempts,
            backoff_factor=factor,
            retry_non_idempotent=retry_non_idempotent,
            transport=transport,
            **kwargs,
        )
    policy: dict[str, Any] = {
        "retries": attempts,
        "backoff_factor": factor,
        "retry_non_idempotent": retry_non_idempotent,
    }
    # An injected transport keeps the retry policy: it is wrapped, never used bare.
    retry_transport = RetryTransport(**policy) if transport is None else RetryTransportWrapper(transport, **policy)
    with httpx.Client(transport=retry_transport, timeout=timeout) as client:
        return client.request(method, url, **kwargs)


def post_with_retry(
    url: str,
    *,
    timeout: float | httpx.Timeout,
    data: dict[str, Any] | None = None,
    json: Any | None = None,
    files: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int | None = None,
    backoff_factor: float | None = None,
    retry_non_idempotent: bool = False,
    deadline: float | None = None,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Response:
    """POST with the shared policy; status retries stay off by default so uploads are not silently duplicated."""
    return request_with_retry(
        "POST",
        url,
        timeout=timeout,
        retries=retries,
        backoff_factor=backoff_factor,
        retry_non_idempotent=retry_non_idempotent,
        deadline=deadline,
        transport=transport,
        data=data,
        json=json,
        files=files,
        headers=headers,
    )


def requests_session_with_retry(
    retries: int | None = None,
    backoff_factor: float | None = None,
    allowed_methods: frozenset[str] = IDEMPOTENT_METHODS,
) -> Any:
    """``requests.Session`` with urllib3 retries matching the shared policy, for the non-httpx callers."""
    import requests  # local import: requests is an optional peer, not a dependency of this library
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    total = default_retries() if retries is None else retries
    retry = Retry(
        total=total,
        connect=total,
        read=total,
        status=total,
        backoff_factor=default_backoff() if backoff_factor is None else backoff_factor,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=allowed_methods,
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


__all__ = (
    "BACKOFF_ENV",
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_RETRIES",
    "IDEMPOTENT_METHODS",
    "MAX_RETRY_AFTER_SECONDS",
    "RETRIES_ENV",
    "RETRYABLE_EXC",
    "RETRYABLE_STATUS",
    "AsyncRetryTransport",
    "RetryTransport",
    "RetryTransportWrapper",
    "bind_django_settings",
    "default_backoff",
    "default_retries",
    "get_async_retry_transport",
    "get_retry_transport",
    "interservice_httpx_args",
    "is_idempotent_method",
    "is_retryable_status",
    "jittered_backoff",
    "parse_retry_after_seconds",
    "post_with_retry",
    "request_with_retry",
    "requests_session_with_retry",
    "should_retry_status",
    "unbind_django_settings",
)
