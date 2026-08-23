"""Retry policy: the disable contract, idempotent gating, Retry-After, jitter and the deadline budget."""

import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from django.test import override_settings

from netix_backend.http import retry


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    """Record sleeps instead of performing them; the module clock stays real."""
    sleeps: list[float] = []
    monkeypatch.setattr(retry.time, "sleep", sleeps.append)

    async def _async_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(retry.asyncio, "sleep", _async_sleep)
    yield sleeps
    retry.unbind_django_settings()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(retry.RETRIES_ENV, raising=False)
    monkeypatch.delenv(retry.BACKOFF_ENV, raising=False)


def _script(monkeypatch, items, *, is_async=False):
    """Replace the socket layer under the transports; each item is a Response or an exception to raise."""
    remaining = list(items)
    calls: list[httpx.Request] = []

    def _next(request):
        calls.append(request)
        item = remaining.pop(0) if remaining else remaining
        if isinstance(item, Exception):
            raise item
        return item

    if is_async:

        async def _handle(self, request):
            return _next(request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _handle)
    else:

        def _handle(self, request):
            return _next(request)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _handle)
    return calls


def _request(method="GET", url="https://svc.internal/api/things?token=secret"):
    return httpx.Request(method, url)


def test_retries_zero_issues_exactly_one_attempt(monkeypatch):
    """Regression guard for `retries or settings...`: 0 must disable, never restore the default."""
    calls = _script(monkeypatch, [httpx.Response(503), httpx.Response(200)])
    transport = retry.RetryTransport(retries=0, backoff_factor=0.0)
    response = transport.handle_request(_request())
    assert response.status_code == 503
    assert len(calls) == 1


def test_status_retry_returns_the_last_response_after_exhaustion(monkeypatch, caplog):
    calls = _script(monkeypatch, [httpx.Response(500), httpx.Response(502), httpx.Response(503)])
    transport = retry.RetryTransport(retries=2, backoff_factor=0.0)
    with caplog.at_level(logging.WARNING):
        response = transport.handle_request(_request())
    assert response.status_code == 503
    assert len(calls) == 3
    assert "token=secret" not in caplog.text


def test_status_retry_stops_at_the_first_success(monkeypatch):
    calls = _script(monkeypatch, [httpx.Response(429), httpx.Response(200)])
    transport = retry.RetryTransport(retries=3, backoff_factor=0.0)
    assert transport.handle_request(_request()).status_code == 200
    assert len(calls) == 2


def test_status_retries_are_gated_on_idempotent_methods(monkeypatch):
    calls = _script(monkeypatch, [httpx.Response(500), httpx.Response(200)])
    transport = retry.RetryTransport(retries=2, backoff_factor=0.0)
    assert transport.handle_request(_request("POST")).status_code == 500
    assert len(calls) == 1


def test_non_idempotent_retry_is_opt_in(monkeypatch):
    calls = _script(monkeypatch, [httpx.Response(500), httpx.Response(201)])
    transport = retry.RetryTransport(retries=2, backoff_factor=0.0, retry_non_idempotent=True)
    assert transport.handle_request(_request("POST")).status_code == 201
    assert len(calls) == 2


def test_a_non_retryable_status_is_returned_untouched(monkeypatch):
    calls = _script(monkeypatch, [httpx.Response(404)])
    transport = retry.RetryTransport(retries=2, backoff_factor=0.0)
    assert transport.handle_request(_request()).status_code == 404
    assert len(calls) == 1


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadError("reset"),
        httpx.PoolTimeout("saturated"),
        httpx.RemoteProtocolError("bad framing"),
    ],
)
def test_transient_exceptions_retry_for_any_method(monkeypatch, exc):
    calls = _script(monkeypatch, [exc, httpx.Response(201)])
    transport = retry.RetryTransport(retries=1, backoff_factor=0.0)
    assert transport.handle_request(_request("POST")).status_code == 201
    assert len(calls) == 2


def test_exception_is_reraised_once_the_budget_is_spent(monkeypatch, caplog):
    calls = _script(monkeypatch, [httpx.ConnectError("refused"), httpx.ConnectError("refused")])
    transport = retry.RetryTransport(retries=1, backoff_factor=0.0)
    with caplog.at_level(logging.ERROR), pytest.raises(httpx.ConnectError):
        transport.handle_request(_request())
    assert len(calls) == 2
    assert "token=secret" not in caplog.text


def test_a_non_retryable_exception_is_not_swallowed(monkeypatch):
    _script(monkeypatch, [httpx.UnsupportedProtocol("nope")])
    transport = retry.RetryTransport(retries=2, backoff_factor=0.0)
    with pytest.raises(httpx.UnsupportedProtocol):
        transport.handle_request(_request())


def test_retry_after_is_honoured_and_clamped(monkeypatch, _no_real_sleeping):
    _script(monkeypatch, [httpx.Response(503, headers={"Retry-After": "900"}), httpx.Response(200)])
    transport = retry.RetryTransport(retries=1, backoff_factor=0.0, max_retry_after=30.0)
    assert transport.handle_request(_request()).status_code == 200
    assert _no_real_sleeping == [30.0]


def test_the_connection_is_released_before_sleeping(monkeypatch):
    closed_before_sleep = []
    response = httpx.Response(503)
    _script(monkeypatch, [response, httpx.Response(200)])
    monkeypatch.setattr(retry.time, "sleep", lambda _delay: closed_before_sleep.append(response.is_closed))
    transport = retry.RetryTransport(retries=1, backoff_factor=0.0)
    assert transport.handle_request(_request()).status_code == 200
    assert closed_before_sleep == [True]


async def test_async_transport_retries_statuses_and_exceptions(monkeypatch):
    calls = _script(
        monkeypatch,
        [httpx.ConnectError("refused"), httpx.Response(503), httpx.Response(200)],
        is_async=True,
    )
    transport = retry.AsyncRetryTransport(retries=2, backoff_factor=0.0)
    response = await transport.handle_async_request(_request())
    assert response.status_code == 200
    assert len(calls) == 3


async def test_async_transport_disables_on_zero_retries(monkeypatch):
    calls = _script(monkeypatch, [httpx.Response(503), httpx.Response(200)], is_async=True)
    transport = retry.AsyncRetryTransport(retries=0, backoff_factor=0.0)
    assert (await transport.handle_async_request(_request())).status_code == 503
    assert len(calls) == 1


async def test_async_transport_skips_non_idempotent_status_retries(monkeypatch):
    calls = _script(monkeypatch, [httpx.Response(500)], is_async=True)
    transport = retry.AsyncRetryTransport(retries=2, backoff_factor=0.0)
    assert (await transport.handle_async_request(_request("POST"))).status_code == 500
    assert len(calls) == 1


async def test_async_transport_reraises_after_exhaustion(monkeypatch):
    _script(monkeypatch, [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow")], is_async=True)
    transport = retry.AsyncRetryTransport(retries=1, backoff_factor=0.0)
    with pytest.raises(httpx.ReadTimeout):
        await transport.handle_async_request(_request())


async def test_async_transport_exhausts_status_retries(monkeypatch):
    _script(monkeypatch, [httpx.Response(503), httpx.Response(503)], is_async=True)
    transport = retry.AsyncRetryTransport(retries=1, backoff_factor=0.0)
    assert (await transport.handle_async_request(_request())).status_code == 503


def test_defaults_come_from_the_shared_env_knobs(monkeypatch):
    assert retry.default_retries() == retry.DEFAULT_RETRIES
    assert retry.default_backoff() == retry.DEFAULT_BACKOFF_FACTOR
    monkeypatch.setenv(retry.RETRIES_ENV, "5")
    monkeypatch.setenv(retry.BACKOFF_ENV, "1.5")
    assert retry.default_retries() == 5
    assert retry.default_backoff() == 1.5
    transport = retry.RetryTransport()
    assert (transport.retries, transport.backoff_factor) == (5, 1.5)


@override_settings(HTTP_CLIENT_RETRIES=7, HTTP_RETRY_BACKOFF_FACTOR=2.5)
def test_bind_django_settings_aliases_legacy_knobs(monkeypatch):
    retry.bind_django_settings(retries_setting="HTTP_CLIENT_RETRIES", backoff_setting="HTTP_RETRY_BACKOFF_FACTOR")
    assert retry.default_retries() == 7
    assert retry.default_backoff() == 2.5
    # Env still wins over the legacy alias.
    monkeypatch.setenv(retry.RETRIES_ENV, "1")
    assert retry.default_retries() == 1
    retry.unbind_django_settings()
    assert retry.default_backoff() == retry.DEFAULT_BACKOFF_FACTOR


def test_bind_django_settings_ignores_unset_names():
    retry.bind_django_settings()
    retry.bind_django_settings(retries_setting="MISSING_SETTING", backoff_setting="ALSO_MISSING")
    assert retry.default_retries() == retry.DEFAULT_RETRIES
    assert retry.default_backoff() == retry.DEFAULT_BACKOFF_FACTOR


@pytest.mark.parametrize(
    ("method", "status", "expected"),
    [
        ("GET", 408, True),
        ("get", 429, True),
        ("HEAD", 500, True),
        ("OPTIONS", 599, True),
        ("GET", 404, False),
        ("GET", 200, False),
        ("POST", 503, False),
        ("DELETE", 503, False),
    ],
)
def test_should_retry_status(method, status, expected):
    assert retry.should_retry_status(method, status) is expected


def test_should_retry_status_with_opt_in():
    assert retry.should_retry_status("POST", 503, retry_non_idempotent=True) is True
    assert retry.should_retry_status("POST", 404, retry_non_idempotent=True) is False


def test_is_idempotent_method_and_status_helpers():
    assert retry.is_idempotent_method("get") is True
    assert retry.is_idempotent_method("PATCH") is False
    assert retry.is_retryable_status(500) is True
    assert retry.is_retryable_status(302) is False


def test_jittered_backoff_stays_inside_the_equal_jitter_band():
    for attempt in range(4):
        cap = 0.5 * 2**attempt
        delay = retry.jittered_backoff(0.5, attempt)
        assert cap / 2 <= delay <= cap


def test_parse_retry_after_seconds_forms():
    assert retry.parse_retry_after_seconds(httpx.Response(503)) is None
    assert retry.parse_retry_after_seconds(httpx.Response(503, headers={"Retry-After": ""})) is None
    assert retry.parse_retry_after_seconds(httpx.Response(503, headers={"Retry-After": "5"})) == 5.0
    assert retry.parse_retry_after_seconds(httpx.Response(503, headers={"Retry-After": "-5"})) == 0.0
    assert retry.parse_retry_after_seconds(httpx.Response(503, headers={"Retry-After": "999"})) == 30.0
    assert (
        retry.parse_retry_after_seconds(httpx.Response(503, headers={"Retry-After": "999"}), max_retry_after=2.0) == 2.0
    )


def test_parse_retry_after_seconds_http_date_form():
    when = datetime.now(UTC) + timedelta(seconds=10)
    header = {"Retry-After": format_datetime(when, usegmt=True)}
    delay = retry.parse_retry_after_seconds(httpx.Response(503, headers=header))
    assert delay is not None and 0.0 < delay <= 10.0
    past = format_datetime(datetime.now(UTC) - timedelta(seconds=10), usegmt=True)
    assert retry.parse_retry_after_seconds(httpx.Response(503, headers={"Retry-After": past})) == 0.0


def test_parse_retry_after_seconds_rejects_garbage():
    assert retry.parse_retry_after_seconds(httpx.Response(503, headers={"Retry-After": "soon"})) is None


def test_transport_factories_build_the_shared_policy():
    assert isinstance(retry.get_retry_transport(retries=1), retry.RetryTransport)
    assert isinstance(retry.get_async_retry_transport(retries=1), retry.AsyncRetryTransport)
    args = retry.interservice_httpx_args(follow_redirects=False)
    assert isinstance(args["transport"], retry.RetryTransport)
    assert args["follow_redirects"] is False


def test_request_with_retry_uses_the_shared_transport(monkeypatch):
    calls = _script(monkeypatch, [httpx.Response(503), httpx.Response(200, text="ok")])
    response = retry.request_with_retry("GET", "https://svc.internal/x", timeout=1.0, retries=1, backoff_factor=0.0)
    assert response.text == "ok"
    assert len(calls) == 2


def test_post_with_retry_does_not_replay_uploads_by_default(monkeypatch):
    calls = _script(monkeypatch, [httpx.Response(503), httpx.Response(201)])
    response = retry.post_with_retry(
        "https://svc.internal/api/static/",
        timeout=1.0,
        data={"name": "r.html"},
        files={"file": ("r.html", b"<p/>", "text/html")},
        headers={"Authorization": "token"},
        retries=2,
        backoff_factor=0.0,
    )
    assert response.status_code == 503
    assert len(calls) == 1


def test_request_with_retry_accepts_an_injected_transport():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    response = retry.request_with_retry(
        "GET",
        "https://svc.internal/x",
        timeout=1.0,
        transport=httpx.MockTransport(handler),
    )
    assert response.json() == {"ok": True}
    assert len(calls) == 1


def test_deadline_path_retries_then_succeeds(monkeypatch):
    responses = [httpx.Response(503), httpx.Response(200, text="late")]
    transport = httpx.MockTransport(lambda request: responses.pop(0))
    response = retry.request_with_retry(
        "GET",
        "https://svc.internal/x",
        timeout=5.0,
        retries=2,
        backoff_factor=0.0,
        deadline=30.0,
        transport=transport,
    )
    assert response.text == "late"


def test_deadline_path_returns_the_last_response_on_exhaustion():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(502)

    response = retry.request_with_retry(
        "GET",
        "https://svc.internal/x",
        timeout=5.0,
        retries=1,
        backoff_factor=0.0,
        deadline=30.0,
        transport=httpx.MockTransport(handler),
    )
    assert response.status_code == 502
    assert len(calls) == 2


def test_deadline_path_reraises_the_last_exception():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        retry.request_with_retry(
            "GET",
            "https://svc.internal/x",
            timeout=5.0,
            retries=1,
            backoff_factor=0.0,
            deadline=30.0,
            transport=httpx.MockTransport(handler),
        )


def test_deadline_path_gives_up_before_the_first_attempt():
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    with pytest.raises(httpx.TimeoutException, match="deadline"):
        retry.request_with_retry(
            "GET",
            "https://svc.internal/x",
            timeout=5.0,
            deadline=0.0,
            transport=transport,
        )
    assert calls == []


def test_deadline_path_with_a_negative_budget_makes_no_attempt():
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    with pytest.raises(httpx.TimeoutException):
        retry.request_with_retry(
            "GET",
            "https://svc.internal/x",
            timeout=5.0,
            retries=-1,
            deadline=30.0,
            transport=transport,
        )
    assert calls == []


def test_deadline_path_stops_when_the_backoff_would_outlive_the_budget(monkeypatch):
    clock = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(retry.time, "monotonic", lambda: next(clock, 100.0))
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    response = retry.request_with_retry(
        "GET",
        "https://svc.internal/x",
        timeout=5.0,
        retries=3,
        backoff_factor=1.0,
        deadline=10.0,
        transport=httpx.MockTransport(handler),
    )
    assert response.status_code == 503
    assert len(calls) == 1


def test_deadline_path_skips_status_gating_for_posts():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    response = retry.post_with_retry(
        "https://svc.internal/x",
        timeout=5.0,
        deadline=30.0,
        retries=2,
        backoff_factor=0.0,
        transport=httpx.MockTransport(handler),
    )
    assert response.status_code == 503
    assert len(calls) == 1


def test_attempt_timeout_never_outlives_the_remaining_budget():
    capped = retry._attempt_timeout(10.0, 2.0)
    assert (capped.connect, capped.read, capped.write, capped.pool) == (2.0, 2.0, 2.0, 2.0)
    unset = retry._attempt_timeout(httpx.Timeout(None), 3.0)
    assert (unset.connect, unset.read, unset.write, unset.pool) == (3.0, 3.0, 3.0, 3.0)
    generous = retry._attempt_timeout(httpx.Timeout(1.0), 30.0)
    assert generous.read == 1.0


def test_requests_session_with_retry_mirrors_the_policy(monkeypatch):
    session = retry.requests_session_with_retry()
    try:
        adapter = session.get_adapter("https://svc.internal")
        assert adapter.max_retries.total == retry.DEFAULT_RETRIES
        assert adapter.max_retries.backoff_factor == retry.DEFAULT_BACKOFF_FACTOR
        assert adapter.max_retries.allowed_methods == retry.IDEMPOTENT_METHODS
        assert adapter.max_retries.status_forcelist == (408, 429, 500, 502, 503, 504)
    finally:
        session.close()
    explicit = retry.requests_session_with_retry(retries=0, backoff_factor=1.0)
    try:
        assert explicit.get_adapter("http://svc.internal").max_retries.total == 0
    finally:
        explicit.close()


def test_an_injected_transport_keeps_the_retry_policy():
    """request_with_retry wraps a caller's transport instead of using it bare, so retries are never silently lost."""
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    response = retry.request_with_retry(
        "GET",
        "https://svc.internal/x",
        timeout=1.0,
        retries=2,
        backoff_factor=0.0,
        transport=httpx.MockTransport(handler),
    )
    assert response.status_code == 503
    assert len(calls) == 3


def test_the_wrapper_closes_the_transport_it_wraps():
    inner = httpx.MockTransport(lambda request: httpx.Response(200))
    closed: list[bool] = []
    inner.close = lambda: closed.append(True)  # type: ignore[method-assign]
    wrapper = retry.RetryTransportWrapper(inner, retries=0)
    with httpx.Client(transport=wrapper) as client:
        assert client.get("https://svc.internal/x").status_code == 200
    assert closed == [True]


def test_post_with_retry_sends_json_bodies() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(200)

    response = retry.post_with_retry(
        "https://svc.test/ack", timeout=1.0, json={"ok": True}, transport=httpx.MockTransport(handler)
    )
    assert response.status_code == 200
    assert seen["content_type"] == "application/json"
    assert b'"ok"' in seen["body"]
