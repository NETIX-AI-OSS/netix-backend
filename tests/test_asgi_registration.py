"""Self-registration with update-service: env contract, retry ladder, heartbeat, once-per-process latch, wrappers."""

import json
import threading
from types import SimpleNamespace

import pytest

from netix_backend.asgi import registration

ENVIRONMENT = {
    "SERVICE_REGISTRATION_ENABLED": "TRUE",
    "SERVICE_NAME": "example-service",
    "SERVICE_DISPLAY_NAME": "Example Service",
    "SERVICE_DESCRIPTION": "Example API",
    "SERVICE_REGISTRATION_ENVIRONMENT": "staging",
    "APP_VERSION": "1.2.3",
    "SERVICE_PUBLIC_BASE_URL": "https://example.api.netixai.dev",
    "SERVICE_INTERNAL_BASE_URL": "http://example-service.backend:8000",
    "UPDATE_SERVICE_REGISTRATION_URL": "http://update-service.backend:8000/api/registry/registration/",
    "SERVICE_REGISTRATION_TOKEN": "test-registration-token",
    "SERVICE_REGISTRATION_HEARTBEAT_SECONDS": "0",
}


class _Response:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _ImmediateThread:
    """Runs the target inline, so the daemon thread's work is asserted on rather than raced with."""

    started = 0

    def __init__(self, *, target, args=(), name="", daemon=False):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon

    def start(self):
        type(self).started += 1
        self.target(*self.args)


@pytest.fixture(autouse=True)
def _clear_latch():
    registration.reset_registration_state()
    _ImmediateThread.started = 0
    yield
    registration.reset_registration_state()


@pytest.fixture
def configured(monkeypatch):
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return ENVIRONMENT


@pytest.fixture
def inline_thread(monkeypatch):
    monkeypatch.setattr(registration, "threading", SimpleNamespace(Thread=_ImmediateThread, Event=threading.Event))
    return _ImmediateThread


def _record_requests(monkeypatch, response=None):
    requests = []
    monkeypatch.setattr(
        registration,
        "urlopen",
        lambda request, timeout: requests.append((request, timeout)) or (response or _Response()),
    )
    return requests


def test_payload_is_disabled_until_the_env_flag_is_set(monkeypatch):
    monkeypatch.delenv(registration.ENABLED_ENV, raising=False)
    assert registration.registration_payload() is None
    assert registration.registration_enabled() is False


def test_payload_names_every_missing_variable(monkeypatch, caplog):
    monkeypatch.setenv(registration.ENABLED_ENV, "TRUE")
    for name in (*registration.REGISTRATION_FIELDS.values(), registration.URL_ENV, registration.TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)
    with caplog.at_level("ERROR"):
        assert registration.registration_payload() is None
    assert "SERVICE_NAME" in caplog.text
    assert registration.URL_ENV in caplog.text
    assert registration.TOKEN_ENV in caplog.text


def test_a_missing_token_alone_is_reported(monkeypatch, configured, caplog):
    monkeypatch.delenv(registration.TOKEN_ENV)
    with caplog.at_level("ERROR"):
        assert registration.registration_payload() is None
    assert registration.TOKEN_ENV in caplog.text


def test_register_puts_the_env_driven_contract(monkeypatch, configured):
    requests = _record_requests(monkeypatch)

    assert registration.register() is True

    request, timeout = requests[0]
    assert request.full_url == configured["UPDATE_SERVICE_REGISTRATION_URL"]
    assert request.method == "PUT"
    assert request.get_header("X-service-registration-token") == configured["SERVICE_REGISTRATION_TOKEN"]
    assert timeout == registration.REGISTRATION_TIMEOUT_SECONDS
    assert json.loads(request.data) == {
        "name": configured["SERVICE_NAME"],
        "display_name": configured["SERVICE_DISPLAY_NAME"],
        "description": configured["SERVICE_DESCRIPTION"],
        "version": configured["APP_VERSION"],
        "environment": configured["SERVICE_REGISTRATION_ENVIRONMENT"],
        "base_url": configured["SERVICE_PUBLIC_BASE_URL"],
        "internal_base_url": configured["SERVICE_INTERNAL_BASE_URL"],
    }


def test_register_is_a_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv(registration.ENABLED_ENV, raising=False)
    monkeypatch.setattr(registration, "urlopen", lambda *_args, **_kwargs: pytest.fail("must not call update-service"))
    assert registration.register() is False


def test_register_retries_along_the_ladder(monkeypatch, configured):
    """The ladder outlasts an update-service that is itself still starting; delays are the whole point."""
    attempts = iter([OSError("not ready"), _Response()])

    def open_after_failure(*_args, **_kwargs):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(registration, "urlopen", open_after_failure)
    assert registration.register(retry_delays=(0, 0.01)) is True


def test_register_gives_up_after_the_last_delay(monkeypatch, configured, caplog):
    _record_requests(monkeypatch, response=_Response(status=503))
    with caplog.at_level("WARNING"):
        assert registration.register(retry_delays=(0,)) is False
    assert "failed after 1 attempts" in caplog.text


def test_the_ladder_is_the_fleet_default():
    assert registration.RETRY_DELAYS == (0, 1, 2, 4, 8, 16, 30, 60)


def test_heartbeat_re_puts_until_the_latch_is_reset(monkeypatch, configured):
    beats = []
    done = threading.Event()

    def beat(request, timeout):
        beats.append(request)
        if len(beats) == 2:
            registration.reset_registration_state()
            done.set()
        return _Response()

    monkeypatch.setattr(registration, "urlopen", beat)
    assert registration.start_heartbeat(interval=0.001) is True

    assert done.wait(5)
    assert all(request.method == "PUT" for request in beats)


def test_a_failed_heartbeat_warns_and_keeps_beating(monkeypatch, configured, caplog):
    outcomes = iter([OSError("update-service down"), _Response(status=503), _Response()])
    done = threading.Event()

    def beat(*_args, **_kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome.status == 200:
            registration.reset_registration_state()
            done.set()
        return outcome

    monkeypatch.setattr(registration, "urlopen", beat)
    with caplog.at_level("WARNING"):
        registration.start_heartbeat(interval=0.001)
        assert done.wait(5)

    assert [record.levelname for record in caplog.records] == ["WARNING", "WARNING"]


def test_the_heartbeat_stops_once_reset_or_unconfigured(monkeypatch, configured):
    monkeypatch.setattr(registration, "urlopen", lambda *_args, **_kwargs: pytest.fail("must not beat"))
    stopped = threading.Event()
    stopped.set()
    registration._heartbeat(60, stopped)  # pylint: disable=protected-access
    monkeypatch.delenv(registration.ENABLED_ENV)
    registration._heartbeat(0.001, threading.Event())  # pylint: disable=protected-access


def test_heartbeat_is_off_at_zero_seconds(configured):
    assert registration.start_heartbeat() is False


def test_an_inline_thread_fake_registers_without_blocking_on_the_heartbeat(monkeypatch, configured):
    """Consumer suites swap the global threading.Thread for an inline fake; the timer chain must not run inline."""
    monkeypatch.setenv(registration.HEARTBEAT_ENV, "3600")
    requests = _record_requests(monkeypatch)

    class _Inline:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(threading, "Thread", _Inline)
    assert registration.trigger_service_registration() is True

    assert len(requests) == 1


def test_the_thread_registers_then_heartbeats(monkeypatch, configured, inline_thread):
    calls = []
    monkeypatch.setattr(registration, "register", lambda retry_delays: calls.append(("register", retry_delays)))
    monkeypatch.setattr(registration, "start_heartbeat", lambda: calls.append(("heartbeat",)))

    registration.trigger_service_registration(retry_delays=(0,))

    assert calls == [("register", (0,)), ("heartbeat",)]


def test_the_thread_does_nothing_when_misconfigured(monkeypatch, inline_thread):
    monkeypatch.setenv(registration.ENABLED_ENV, "TRUE")
    monkeypatch.delenv(registration.URL_ENV, raising=False)
    monkeypatch.setattr(registration, "register", lambda *_args: pytest.fail("must not register"))

    assert registration.trigger_service_registration() is True
    assert inline_thread.started == 1


def test_trigger_starts_registration_exactly_once(monkeypatch, configured, inline_thread):
    _record_requests(monkeypatch)

    assert registration.trigger_service_registration() is True
    assert registration.trigger_service_registration() is False

    assert inline_thread.started == 1
    assert registration.registration_started() is True


def test_trigger_is_a_noop_while_registration_is_disabled(monkeypatch, inline_thread):
    monkeypatch.setenv(registration.ENABLED_ENV, "FALSE")
    assert registration.trigger_service_registration() is False
    assert inline_thread.started == 0


def test_reset_releases_the_latch(monkeypatch, configured, inline_thread):
    _record_requests(monkeypatch)
    registration.trigger_service_registration()
    registration.reset_registration_state()
    assert registration.registration_started() is False
    assert registration.trigger_service_registration() is True
    assert inline_thread.started == 2


async def test_the_asgi_wrapper_triggers_only_for_http(monkeypatch):
    scopes = []
    triggers = []
    monkeypatch.setattr(registration, "trigger_service_registration", lambda: triggers.append(True))

    async def application(scope, _receive, _send):
        scopes.append(scope["type"])

    wrapper = registration.ServiceRegistrationASGI(application)
    await wrapper({"type": "lifespan"}, None, None)
    await wrapper({"type": "http"}, None, None)

    assert scopes == ["lifespan", "http"]
    assert triggers == [True]


def test_the_wsgi_wrapper_triggers_and_delegates(monkeypatch):
    triggers = []
    delegated = []
    monkeypatch.setattr(registration, "trigger_service_registration", lambda: triggers.append(True))

    def application(environ, start_response):
        delegated.append((environ, start_response))
        return [b"ok"]

    start_response = object()
    environ = {"PATH_INFO": "/healthz/"}
    wrapper = registration.ServiceRegistrationWSGI(application)

    assert wrapper(environ, start_response) == [b"ok"]
    assert triggers == [True]
    assert delegated == [(environ, start_response)]


def test_the_module_is_re_exported_for_wsgi_only_services():
    from netix_backend import registration as top_level

    assert top_level.trigger_service_registration is registration.trigger_service_registration
    assert top_level.ServiceRegistrationWSGI is registration.ServiceRegistrationWSGI
