"""The Redis/Sentinel CACHES factory: both branches, the django_redis knobs, and the strict/lenient env fork."""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from netix_backend.django.cache import (
    DEFAULT_CLIENT,
    DEFAULT_CONNECTION_FACTORY,
    SENTINEL_CLIENT,
    SENTINEL_CONNECTION_FACTORY,
    SENTINEL_CONNECTION_POOL,
    cache_ttls,
    redis_caches,
)
from netix_backend.env import ConfigurationError

REDIS_ENV = {
    "SENTINEL_ENABLED": "FALSE",
    "REDIS_HOST": "redis.svc",
    "REDIS_PORT": "6379",
    "REDIS_DB": "3",
    "REDIS_PASSWORD": "hunter2",
    "SENTINEL_HOST": "sentinel-0",
    "SENTINEL_PORT": "26379",
    "SENTINEL_SERVICE": "mymaster",
    "CACHE_TTL": "120",
    "CACHE_ENABLED": "TRUE",
}


@pytest.fixture(autouse=True)
def redis_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REDIS_ENV.items():
        monkeypatch.setenv(name, value)


def options(**kwargs: object) -> dict:
    return redis_caches(**kwargs).caches["default"]["OPTIONS"]  # type: ignore[arg-type]


class TestPlainRedisBranch:
    def test_location_is_built_from_host_port_and_db(self) -> None:
        settings = redis_caches(sentinel_enabled=False)
        assert settings.caches["default"]["LOCATION"] == "redis://redis.svc:6379/3"
        assert settings.caches["default"]["BACKEND"] == "django_redis.cache.RedisCache"

    def test_uses_the_default_client_and_factory(self) -> None:
        settings = redis_caches(sentinel_enabled=False)
        assert settings.caches["default"]["OPTIONS"]["CLIENT_CLASS"] == DEFAULT_CLIENT
        # django_redis' own default, bound unconditionally so settings.py never has to guard the else branch.
        assert settings.connection_factory == DEFAULT_CONNECTION_FACTORY
        assert settings.sentinels == []
        assert settings.sentinel_hosts == []

    def test_the_flag_is_read_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SENTINEL_ENABLED", "TRUE")
        assert redis_caches().connection_factory == SENTINEL_CONNECTION_FACTORY

    def test_explicit_arguments_win_over_the_environment(self) -> None:
        settings = redis_caches(sentinel_enabled=False, host="localhost", port=6380, db=9, password="")
        assert settings.caches["default"]["LOCATION"] == "redis://localhost:6380/9"
        assert settings.caches["default"]["OPTIONS"]["PASSWORD"] == ""

    def test_a_second_alias_is_addressable(self) -> None:
        assert set(redis_caches(sentinel_enabled=False, alias="sessions").caches) == {"sessions"}


class TestSentinelBranch:
    def test_location_is_built_from_the_sentinel_service(self) -> None:
        settings = redis_caches(sentinel_enabled=True)
        assert settings.caches["default"]["LOCATION"] == "redis://mymaster/3"
        assert settings.connection_factory == SENTINEL_CONNECTION_FACTORY

    def test_the_single_host_form_is_the_default(self) -> None:
        settings = redis_caches(sentinel_enabled=True)
        assert settings.sentinels == [("sentinel-0", "26379")]
        assert settings.sentinel_hosts == ["sentinel-0"]

    def test_sentinel_options(self) -> None:
        resolved = options(sentinel_enabled=True)
        assert resolved["CLIENT_CLASS"] == SENTINEL_CLIENT
        assert resolved["CONNECTION_POOL_CLASS"] == SENTINEL_CONNECTION_POOL
        assert resolved["SENTINELS"] == [("sentinel-0", "26379")]
        assert resolved["SENTINEL_KWARGS"] == {"password": "hunter2"}

    def test_a_multi_host_list_is_opt_in(self) -> None:
        # tag-service's guard against MasterNotFoundError when one sentinel has not converged.
        settings = redis_caches(sentinel_enabled=True, sentinel_hosts=["s-0", "s-1"])
        assert settings.sentinels == [("s-0", "26379"), ("s-1", "26379")]

    def test_a_comma_separated_string_is_split_and_stripped(self) -> None:
        settings = redis_caches(sentinel_enabled=True, sentinel_hosts="s-0, s-1 ,")
        assert settings.sentinel_hosts == ["s-0", "s-1"]

    def test_no_resolved_host_is_a_configuration_failure(self) -> None:
        with pytest.raises(ImproperlyConfigured, match="resolved to no hosts"):
            redis_caches(sentinel_enabled=True, sentinel_hosts=" , ")


class TestEnvStyle:
    def test_strict_raises_on_a_missing_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("REDIS_HOST")
        with pytest.raises(ConfigurationError, match="REDIS_HOST"):
            redis_caches(sentinel_enabled=False)

    def test_lenient_falls_back_to_the_local_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("REDIS_HOST", "REDIS_PORT", "REDIS_DB", "REDIS_PASSWORD"):
            monkeypatch.delenv(name)
        settings = redis_caches(sentinel_enabled=False, strict=False)
        assert settings.caches["default"]["LOCATION"] == "redis://localhost:6379/0"
        assert settings.caches["default"]["OPTIONS"]["PASSWORD"] == ""

    def test_lenient_sentinel_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("SENTINEL_HOST", "SENTINEL_PORT", "SENTINEL_SERVICE"):
            monkeypatch.delenv(name)
        settings = redis_caches(sentinel_enabled=True, strict=False)
        assert settings.sentinels == [("localhost", "26379")]
        assert settings.caches["default"]["LOCATION"] == "redis://mymaster/3"

    def test_strict_reads_the_flag_as_the_exact_true_spelling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SENTINEL_ENABLED", "true")
        assert redis_caches().connection_factory == DEFAULT_CONNECTION_FACTORY

    def test_lenient_accepts_any_boolean_spelling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SENTINEL_ENABLED", "true")
        assert redis_caches(strict=False).connection_factory == SENTINEL_CONNECTION_FACTORY

    def test_strict_requires_the_flag_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SENTINEL_ENABLED")
        with pytest.raises(ConfigurationError, match="SENTINEL_ENABLED"):
            redis_caches()

    def test_an_empty_password_is_not_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_PASSWORD", "")
        assert options(sentinel_enabled=False)["PASSWORD"] == ""


class TestIgnoreExceptions:
    def test_defaults_to_todays_fleet_behaviour(self) -> None:
        settings = redis_caches(sentinel_enabled=False)
        assert settings.ignore_exceptions is False
        assert settings.caches["default"]["OPTIONS"]["IGNORE_EXCEPTIONS"] is False

    def test_the_whole_django_redis_knob_set_comes_back(self) -> None:
        settings = redis_caches(sentinel_enabled=True, ignore_exceptions=True, logger_name="cache")
        assert settings.ignore_exceptions is True
        assert settings.log_ignored_exceptions is True
        assert settings.logger_name == "cache"
        # OPTIONS is read before the module-level name, so both have to agree.
        assert settings.caches["default"]["OPTIONS"]["IGNORE_EXCEPTIONS"] is True

    def test_logging_the_swallowed_exception_can_be_turned_off(self) -> None:
        assert redis_caches(sentinel_enabled=False, log_ignored_exceptions=False).log_ignored_exceptions is False


class TestCacheTtls:
    def test_reads_cache_ttl_from_the_environment(self) -> None:
        assert cache_ttls().ttl == 120

    def test_falls_back_to_the_fleet_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CACHE_TTL")
        assert cache_ttls().ttl == 3600

    def test_an_explicit_ttl_wins(self) -> None:
        assert cache_ttls(ttl=300).ttl == 300

    def test_a_service_specific_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CACHE_TTL")
        assert cache_ttls(default_ttl=300).ttl == 300

    def test_long_ttl_is_absent_unless_asked_for(self) -> None:
        # Five services have no CACHE_LONG_TTL to bind; the field stays None rather than inventing one.
        assert cache_ttls().long_ttl is None

    def test_long_ttl_can_be_derived(self) -> None:
        assert cache_ttls(long_ttl_factor=24).long_ttl == 120 * 24

    def test_an_explicit_long_ttl_wins_over_the_factor(self) -> None:
        assert cache_ttls(long_ttl=3600, long_ttl_factor=24).long_ttl == 3600

    def test_enabled_is_read_from_the_environment(self) -> None:
        assert cache_ttls().enabled is True

    def test_enabled_can_be_pinned(self) -> None:
        assert cache_ttls(enabled=False).enabled is False
