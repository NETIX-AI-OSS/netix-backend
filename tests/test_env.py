"""Environment parsing: the spellings ConfigMaps actually ship, and the fail-loud variants."""

import logging
import os
import subprocess
import sys

import pytest

from netix_backend.env import (
    ConfigurationError,
    env_bool,
    env_bool_strict,
    env_first,
    env_float,
    env_int,
    env_str,
    parse_bool,
    service_authorization,
)

IMPORT_GUARD = (
    "import sys; import netix_backend.env, netix_backend.http; "
    "assert not [m for m in sys.modules if m.split('.')[0] == 'django'], sorted(sys.modules)"
)

TRUE_SPELLINGS = ("1", "TRUE", "true", "True", "yes", "YES", "on", "ON", " 1 ")
FALSE_SPELLINGS = ("0", "FALSE", "false", "False", "no", "off", " 0 ")


@pytest.mark.parametrize("value", TRUE_SPELLINGS)
def test_env_bool_accepts_the_spellings_the_configmaps_use(monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert env_bool("FLAG") is True


@pytest.mark.parametrize("value", FALSE_SPELLINGS)
def test_env_bool_rejects_falsey_spellings(monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert env_bool("FLAG", default=True) is False


def test_env_bool_regression_configured_one_is_not_read_as_false(monkeypatch):
    """Regression: the old `== "TRUE"` expression read the configured '1' as False."""
    monkeypatch.setenv("NOTIFICATION_SVC_VERIFY_SSL", "1")
    assert env_bool("NOTIFICATION_SVC_VERIFY_SSL") is True


def test_env_bool_defaults_when_unset_or_empty(monkeypatch):
    monkeypatch.delenv("FLAG", raising=False)
    assert env_bool("FLAG") is False
    assert env_bool("FLAG", default=True) is True
    monkeypatch.setenv("FLAG", "   ")
    assert env_bool("FLAG", default=True) is True


def test_env_bool_unrecognised_value_falls_back(monkeypatch):
    monkeypatch.setenv("FLAG", "maybe")
    assert env_bool("FLAG", default=True) is True
    assert env_bool("FLAG", default=False) is False


def test_env_bool_accepts_a_str_default_shim(monkeypatch):
    monkeypatch.delenv("FLAG", raising=False)
    assert env_bool("FLAG", default="TRUE") is True
    assert env_bool("FLAG", default="0") is False


def test_parse_bool_handles_none():
    assert parse_bool(None) is False
    assert parse_bool(None, default=True) is True
    assert parse_bool("on") is True
    assert parse_bool("off", default=True) is False


def test_env_bool_strict_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("NOTIFICATIONS_ENABLED", raising=False)
    assert env_bool_strict("NOTIFICATIONS_ENABLED", default=True) is True


@pytest.mark.parametrize(("value", "expected"), [("YES", True), ("off", False)])
def test_env_bool_strict_parses_known_values(monkeypatch, value, expected):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", value)
    assert env_bool_strict("NOTIFICATIONS_ENABLED", default=not expected) is expected


def test_env_bool_strict_logs_and_fails_open(monkeypatch, caplog):
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "maybe")
    with caplog.at_level(logging.ERROR):
        assert env_bool_strict("NOTIFICATIONS_ENABLED", default=True) is True
    assert "NOTIFICATIONS_ENABLED" in caplog.text
    assert "maybe" in caplog.text
    assert "true" in caplog.text and "false" in caplog.text


def test_env_bool_strict_uses_the_supplied_logger(monkeypatch):
    records = []
    log = logging.getLogger("netix.test.strict")
    log.addHandler(logging.NullHandler())
    monkeypatch.setattr(log, "error", lambda *args, **kwargs: records.append(args))
    monkeypatch.setenv("FLAG", "nope")
    assert env_bool_strict("FLAG", default=False, log=log) is False
    assert records


def test_env_str(monkeypatch):
    monkeypatch.setenv("SVC_URL", " http://svc ")
    assert env_str("SVC_URL") == "http://svc"
    monkeypatch.delenv("SVC_URL", raising=False)
    assert env_str("SVC_URL") is None
    assert env_str("SVC_URL", "fallback") == "fallback"


def test_env_str_required_raises_configuration_error(monkeypatch):
    monkeypatch.setenv("SVC_URL", "")
    with pytest.raises(ConfigurationError, match="SVC_URL"):
        env_str("SVC_URL", required=True)


def test_env_first_is_first_wins(monkeypatch):
    monkeypatch.delenv("PRIMARY", raising=False)
    monkeypatch.setenv("SECONDARY", "second")
    assert env_first("PRIMARY", "SECONDARY") == "second"
    monkeypatch.setenv("PRIMARY", "first")
    assert env_first("PRIMARY", "SECONDARY") == "first"


def test_env_first_default_and_required(monkeypatch):
    monkeypatch.delenv("PRIMARY", raising=False)
    monkeypatch.delenv("SECONDARY", raising=False)
    assert env_first("PRIMARY", "SECONDARY") is None
    assert env_first("PRIMARY", "SECONDARY", default="d") == "d"
    with pytest.raises(ConfigurationError, match="PRIMARY or SECONDARY"):
        env_first("PRIMARY", "SECONDARY", required=True)


def test_env_int(monkeypatch):
    monkeypatch.delenv("RETRIES", raising=False)
    assert env_int("RETRIES", 2) == 2
    monkeypatch.setenv("RETRIES", "  ")
    assert env_int("RETRIES", 2) == 2
    monkeypatch.setenv("RETRIES", "0")
    assert env_int("RETRIES", 2) == 0


def test_env_int_rejects_garbage(monkeypatch):
    monkeypatch.setenv("RETRIES", "two")
    with pytest.raises(ConfigurationError, match="not an integer"):
        env_int("RETRIES", 2)


def test_env_float(monkeypatch):
    monkeypatch.delenv("BACKOFF", raising=False)
    assert env_float("BACKOFF", 0.5) == 0.5
    monkeypatch.setenv("BACKOFF", "")
    assert env_float("BACKOFF", 0.5) == 0.5
    monkeypatch.setenv("BACKOFF", "1.25")
    assert env_float("BACKOFF", 0.5) == 1.25


def test_env_float_rejects_garbage(monkeypatch):
    monkeypatch.setenv("BACKOFF", "slow")
    with pytest.raises(ConfigurationError, match="not a number"):
        env_float("BACKOFF", 0.5)


def test_service_authorization_prefers_the_named_variable(monkeypatch):
    monkeypatch.setenv("STATIC_SVC_AUTH", "static-token")
    monkeypatch.setenv("USER_SVC_AUTH", "shared-token")
    assert service_authorization("STATIC_SVC_AUTH") == "static-token"


def test_service_authorization_falls_back_to_the_platform_token(monkeypatch):
    monkeypatch.delenv("NOTIFICATION_SVC_AUTH", raising=False)
    monkeypatch.setenv("USER_SVC_AUTH", "shared-token")
    assert service_authorization("NOTIFICATION_SVC_AUTH") == "shared-token"


def test_service_authorization_raises_rather_than_returning_empty(monkeypatch):
    monkeypatch.delenv("STATIC_SVC_AUTH", raising=False)
    monkeypatch.setenv("USER_SVC_AUTH", "  ")
    with pytest.raises(ConfigurationError, match="STATIC_SVC_AUTH or USER_SVC_AUTH"):
        service_authorization("STATIC_SVC_AUTH")


def test_service_authorization_without_a_fallback(monkeypatch):
    monkeypatch.setenv("USER_SVC_AUTH", "shared-token")
    monkeypatch.setenv("SCENARIO_AUTH", "scenario-token")
    assert service_authorization("SCENARIO_AUTH", fallback="") == "scenario-token"
    monkeypatch.delenv("SCENARIO_AUTH")
    with pytest.raises(ConfigurationError):
        service_authorization("SCENARIO_AUTH", fallback="")


def test_env_and_http_import_without_pulling_in_django():
    """Migrations and plain scripts import these modules, so nothing here may touch django at import time."""
    environ = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    subprocess.run([sys.executable, "-c", IMPORT_GUARD], check=True, env=environ)
