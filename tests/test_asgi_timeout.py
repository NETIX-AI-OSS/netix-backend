"""The request ceiling's knobs: the ones the shared contract suite cannot express."""

import asyncio
import json

import pytest
from django.conf import LazySettings
from django.test import override_settings

from netix_backend.asgi.testing import drive, slow_app
from netix_backend.asgi.timeout import (
    REQUEST_TIMEOUT_SECONDS,
    RequestTimeoutMiddleware,
    cors_headers,
    django_setting,
    error_messages,
    text_messages,
)


def _reply(method, delay=0.2, **kwargs):
    async def scenario():
        finished = asyncio.Event()
        return await drive(RequestTimeoutMiddleware(slow_app(delay, finished), **kwargs), method, finished)

    return asyncio.run(scenario())


def test_django_setting_reads_a_configured_project():
    with override_settings(CORS_ALLOW_ALL_ORIGINS=True):
        assert django_setting("CORS_ALLOW_ALL_ORIGINS", False) is True
    assert django_setting("NOT_A_REAL_SETTING", "fallback") == "fallback"


def test_django_setting_degrades_while_django_is_unconfigured(monkeypatch):
    """The package has to import and construct in a pre-django.setup() boot path."""
    monkeypatch.setattr("django.conf.settings", LazySettings())
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    assert django_setting("CORS_ALLOW_ALL_ORIGINS", "unset") == "unset"


def test_cors_headers_ignore_requests_carrying_other_headers():
    assert cors_headers({"headers": [(b"host", b"testserver")]}) == []


def test_error_messages_carry_the_bare_string_envelope():
    """messages is a string here, not the list the DRF handler flattens; clients parse both shapes."""
    start, body = error_messages(504, "nope", {})
    assert json.loads(body["body"]) == {"status_code": 504, "messages": "nope"}
    assert (b"content-type", b"application/json") in start["headers"]
    assert not any(name == b"retry-after" for name, _ in start["headers"])


def test_text_messages_reproduce_the_pre_envelope_reply():
    """cafm-backend and simulator-service answer text/plain today; the renderer knob keeps that on adoption."""
    start, body = text_messages(504, "Request exceeded timeout", {}, retry_after=5)
    assert start["headers"] == [(b"content-type", b"text/plain; charset=utf-8"), (b"retry-after", b"5")]
    assert body["body"] == b"Request exceeded timeout"


def test_none_safe_methods_cap_every_method():
    """The sentinel cafm-backend and simulator-service need; an empty set would disable the ceiling instead."""
    assert _reply("POST", timeout=0.05, safe_methods=None).status == 504


def test_an_empty_safe_method_set_disables_the_ceiling():
    assert _reply("GET", timeout=0.05, safe_methods=frozenset()).status == 200


def test_never_shed_is_configurable():
    async def scenario():
        finished = asyncio.Event()
        middleware = RequestTimeoutMiddleware(
            slow_app(0, finished), timeout=0.05, max_abandoned=0, never_shed=frozenset()
        )
        return await drive(middleware, "OPTIONS", finished)

    assert asyncio.run(scenario()).status == 503


def test_a_custom_error_body_replaces_the_envelope():
    reply = _reply("GET", timeout=0.05, error_body=text_messages)
    assert reply.status == 504
    assert reply.headers[b"content-type"] == b"text/plain; charset=utf-8"


def test_the_shed_reply_can_drop_retry_after():
    async def scenario():
        finished = asyncio.Event()
        middleware = RequestTimeoutMiddleware(slow_app(2, finished), timeout=0.05, max_abandoned=0, retry_after=None)
        return await drive(middleware, "GET", finished)

    reply = asyncio.run(scenario())
    assert reply.status == 503
    assert b"retry-after" not in reply.headers


def test_a_django_setting_can_supply_the_ceiling():
    """cafm-backend resolves settings.REQUEST_TIMEOUT lazily; the knob keeps that ceiling after adoption."""
    with override_settings(REQUEST_TIMEOUT=0.25):
        assert RequestTimeoutMiddleware(None, timeout_setting="REQUEST_TIMEOUT").timeout == 0.25


def test_a_missing_setting_falls_back_to_the_module_ceiling():
    assert RequestTimeoutMiddleware(None, timeout_setting="NOT_SET_ANYWHERE").timeout == REQUEST_TIMEOUT_SECONDS


def test_an_explicit_timeout_wins_over_the_setting():
    with override_settings(REQUEST_TIMEOUT=0.25):
        assert RequestTimeoutMiddleware(None, timeout=7, timeout_setting="REQUEST_TIMEOUT").timeout == 7


def test_max_abandoned_defaults_to_the_module_constant():
    assert RequestTimeoutMiddleware(None).max_abandoned == 50


def test_an_app_error_is_re_raised_for_the_server_to_handle():
    async def boom(scope, receive, send):
        raise RuntimeError("view exploded")

    with pytest.raises(RuntimeError, match="view exploded"):
        asyncio.run(RequestTimeoutMiddleware(boom)({"type": "http", "method": "GET"}, None, None))


def test_a_cancelled_abandoned_request_is_still_logged(caplog):
    """The done callback must retrieve the outcome even when the loop tore the task down."""

    async def scenario():
        finished = asyncio.Event()
        middleware = RequestTimeoutMiddleware(slow_app(5, finished), timeout=0.05)
        reply = await drive(middleware, "GET", finished)
        abandoned = next(iter(middleware._abandoned))
        abandoned.cancel()
        await asyncio.gather(abandoned, return_exceptions=True)
        await asyncio.sleep(0)
        return reply

    with caplog.at_level("WARNING"):
        assert asyncio.run(scenario()).status == 504
    assert "it finished later (error=None)" in caplog.text
