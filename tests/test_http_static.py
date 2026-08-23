"""static-service helpers: the 200-vs-201 dedup contract, required retention, 404 expiry and raising failures."""

from datetime import UTC, date, datetime

import httpx
import pytest

from netix_backend.env import ConfigurationError
from netix_backend.http import static

RETENTION = datetime(2027, 1, 1, tzinfo=UTC)
UPLOADED = {"id": 7, "name": "report.html", "link": "https://static.internal/api/file/abc"}


@pytest.fixture(autouse=True)
def _static_env(monkeypatch):
    monkeypatch.setenv("STATIC_SVC_URL", "https://static.internal")
    monkeypatch.setenv("STATIC_SVC_AUTH", "static-token")
    monkeypatch.setenv("STATIC_SVC_TIMEOUT", "7")
    monkeypatch.setenv("STATIC_SVC_VERIFY_SSL", "1")
    monkeypatch.delenv("NETIX_HTTP_RETRIES", raising=False)
    monkeypatch.delenv("NETIX_HTTP_RETRY_BACKOFF", raising=False)


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    monkeypatch.setattr(static.time, "sleep", lambda _delay: None)


def _transport(*responses, record=None):
    queue = list(responses)

    def handler(request):
        if record is not None:
            record.append(request)
        return queue.pop(0)

    return httpx.MockTransport(handler)


def _upload(*responses, record=None, **kwargs):
    return static.static_upload(
        name="report.html",
        content=b"<p>hi</p>",
        mime="text/html",
        retention_date=RETENTION,
        transport=_transport(*responses, record=record),
        **kwargs,
    )


def test_upload_accepts_201_created():
    payload = _upload(httpx.Response(201, json=UPLOADED))
    assert payload["link"] == UPLOADED["link"]
    # `file` is write-only server side, so the generated model needs the link copied in.
    assert payload["file"] == UPLOADED["link"]


def test_upload_accepts_200_the_global_dedup_hit():
    """static-service returns 200 when the checksum already exists; treating that as failure emptied report links."""
    payload = _upload(httpx.Response(200, json=UPLOADED))
    assert payload["link"] == UPLOADED["link"]


def test_upload_posts_the_documented_payload_shape():
    seen: list[httpx.Request] = []
    _upload(httpx.Response(201, json=UPLOADED), record=seen, description="monthly")
    request = seen[0]
    assert request.url.path == static.UPLOAD_PATH
    assert request.headers["Authorization"] == "static-token"
    body = request.content.decode()
    assert 'name="name"' in body and "report.html" in body
    assert "text/html" in body
    assert "2027-01-01T00:00:00+00:00" in body
    assert "monthly" in body


@pytest.mark.parametrize(
    ("retention", "expected"),
    [(date(2027, 5, 4), "2027-05-04"), ("2027-05-04", "2027-05-04")],
)
def test_upload_serialises_every_retention_form(retention, expected):
    seen: list[httpx.Request] = []
    static.static_upload(
        name="r.html",
        content=b"x",
        mime="text/html",
        retention_date=retention,
        transport=_transport(httpx.Response(201, json=UPLOADED), record=seen),
    )
    assert expected in seen[0].content.decode()


def test_upload_rejects_a_response_without_a_link():
    with pytest.raises(static.StaticServiceError, match="no 'link'"):
        _upload(httpx.Response(201, json={"id": 7}))


def test_upload_raises_on_a_client_error_without_retrying():
    seen: list[httpx.Request] = []
    with pytest.raises(static.StaticServiceError, match="status=400"):
        _upload(httpx.Response(400, text="bad name"), record=seen)
    assert len(seen) == 1


def test_upload_retries_a_5xx_then_succeeds():
    seen: list[httpx.Request] = []
    payload = _upload(httpx.Response(503), httpx.Response(201, json=UPLOADED), record=seen)
    assert payload["link"] == UPLOADED["link"]
    assert len(seen) == 2


def test_upload_raises_after_exhausting_retries():
    seen: list[httpx.Request] = []
    with pytest.raises(static.StaticServiceError, match="attempt 2/2"):
        _upload(httpx.Response(500), httpx.Response(502), record=seen, retries=1)
    assert len(seen) == 2


def test_upload_with_a_negative_retry_budget_makes_no_attempt():
    with pytest.raises(static.StaticServiceError, match="made no attempts"):
        _upload(httpx.Response(201, json=UPLOADED), retries=-1)


def test_link_or_empty_returns_the_link_and_swallows_failures(caplog):
    assert (
        static.link_or_empty(
            name="r.html",
            content=b"x",
            mime="text/html",
            retention_date=RETENTION,
            transport=_transport(httpx.Response(200, json=UPLOADED)),
        )
        == UPLOADED["link"]
    )
    assert (
        static.link_or_empty(
            name="r.html",
            content=b"x",
            mime="text/html",
            retention_date=RETENTION,
            transport=_transport(httpx.Response(400, text="nope")),
        )
        == ""
    )
    assert "static-service upload failed" in caplog.text


def test_fetch_returns_bytes_and_text():
    assert static.static_fetch("/api/file/abc", transport=_transport(httpx.Response(200, text="body"))) == b"body"
    assert static.static_fetch_text("/api/file/abc", transport=_transport(httpx.Response(200, text="body"))) == "body"


def test_fetch_maps_404_to_expiry():
    with pytest.raises(static.StaticFileExpired, match="past retention"):
        static.static_fetch("/api/file/gone", transport=_transport(httpx.Response(404)))


def test_fetch_raises_on_a_client_error():
    with pytest.raises(static.StaticServiceError, match="status=403"):
        static.static_fetch("/api/file/abc", transport=_transport(httpx.Response(403, text="denied")))


def test_fetch_retries_a_5xx_then_succeeds():
    seen: list[httpx.Request] = []
    body = static.static_fetch(
        "/api/file/abc",
        transport=_transport(httpx.Response(500), httpx.Response(200, text="body"), record=seen),
    )
    assert body == b"body"
    assert len(seen) == 2


def test_fetch_raises_after_exhausting_retries():
    with pytest.raises(static.StaticServiceError, match="attempt 2/2"):
        static.static_fetch(
            "/api/file/abc",
            retries=1,
            transport=_transport(httpx.Response(502), httpx.Response(503)),
        )


def test_fetch_with_a_negative_retry_budget_makes_no_attempt():
    with pytest.raises(static.StaticServiceError, match="made no attempts"):
        static.static_fetch("/api/file/abc", retries=-1, transport=_transport(httpx.Response(200)))


def test_an_injected_client_is_reused_and_left_open():
    client = httpx.Client(
        base_url="https://static.internal",
        transport=_transport(httpx.Response(201, json=UPLOADED)),
    )
    payload = static.static_upload(
        name="r.html",
        content=b"x",
        mime="text/html",
        retention_date=RETENTION,
        client=client,
    )
    assert payload["link"] == UPLOADED["link"]
    assert client.is_closed is False
    client.close()


def test_build_static_client_reads_its_configuration_from_the_env():
    client = static.build_static_client()
    try:
        assert str(client.base_url) == "https://static.internal"
        assert client.headers["Authorization"] == "static-token"
        assert client.timeout == httpx.Timeout(7.0)
    finally:
        client.close()
    explicit = static.build_static_client(timeout=1.5)
    try:
        assert explicit.timeout == httpx.Timeout(1.5)
    finally:
        explicit.close()


def test_build_static_client_requires_the_service_url(monkeypatch):
    monkeypatch.delenv("STATIC_SVC_URL")
    with pytest.raises(ConfigurationError, match="STATIC_SVC_URL"):
        static.build_static_client()


def test_upload_retries_a_transport_failure_and_reports_it_as_a_static_error():
    """A connect error must surface as StaticServiceError, or link_or_empty's callers stop failing softly."""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        raise httpx.ConnectError("no route to static-service")

    with pytest.raises(static.StaticServiceError, match="could not reach the service"):
        static.static_upload(
            name="report.html",
            content=b"<p/>",
            mime="text/html",
            retention_date=RETENTION,
            retries=1,
            transport=httpx.MockTransport(handler),
        )
    assert len(seen) == 2


def test_fetch_retries_a_transport_failure_and_reports_it_as_a_static_error():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout("static-service is wedged")

    with pytest.raises(static.StaticServiceError, match="could not reach the service"):
        static.static_fetch("/api/file/abc", retries=1, transport=httpx.MockTransport(handler))
    assert len(seen) == 2


def test_link_or_empty_swallows_a_transport_failure(caplog):
    def handler(request):
        raise httpx.ConnectError("no route to static-service")

    assert (
        static.link_or_empty(
            name="r.html",
            content=b"x",
            mime="text/html",
            retention_date=RETENTION,
            retries=0,
            transport=httpx.MockTransport(handler),
        )
        == ""
    )
    assert "static-service upload failed" in caplog.text


def test_the_default_client_does_not_retry_underneath_the_helper_loop(monkeypatch):
    """The helpers own the retry budget; a retrying transport under them would square the attempt count."""
    seen: list[httpx.Request] = []

    def handle(self, request):
        seen.append(request)
        return httpx.Response(503)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    with pytest.raises(static.StaticServiceError):
        static.static_fetch("https://static.internal/api/file/abc", retries=2)
    assert len(seen) == 3
