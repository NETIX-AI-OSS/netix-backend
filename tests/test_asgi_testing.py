"""The shared contract suite, run against this library's own middleware in both knob positions."""

from netix_backend.asgi.testing import RequestTimeoutContract, ServerCycle


class TestContractDefaults(RequestTimeoutContract):
    """The shape 13 gen-3 repos adopt as-is: no urlconf, no db_timeout, MIDDLEWARE check left to the repo."""


class TestContractOptIns(RequestTimeoutContract):
    """Every opt-in knob on, the way a repo with app/db_timeout.py and a slow-view urlconf configures it."""

    urlconf = "tests.asgi_slow_urls"
    forbid_middleware_entry = True
    db_statement_timeout_ms = 30000


async def test_server_cycle_waits_for_the_final_body_chunk():
    """A streamed response only completes on the chunk without more_body, exactly as uvicorn's cycle does."""
    cycle = ServerCycle()
    await cycle.send({"type": "http.response.start", "status": 200})
    await cycle.send({"type": "http.response.body", "body": b"chunk", "more_body": True})
    assert cycle.response_complete is False
    assert cycle.statuses == [200]
    assert await cycle.receive() == {"type": "http.request", "body": b"", "more_body": False}

    await cycle.send({"type": "http.response.body", "body": b"end"})
    assert await cycle.receive() == {"type": "http.disconnect"}
