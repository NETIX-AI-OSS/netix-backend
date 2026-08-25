"""The per-connection statement_timeout receiver ten repos ship a two-line-different copy of."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import pytest
from django.db.backends.base.base import NO_DB_ALIAS
from django.db.backends.signals import connection_created

from netix_backend.django import db_timeout
from netix_backend.django.testing import assert_timeout_invariant
from netix_backend.env import ConfigurationError


class FakeCursor:
    def __init__(self, sink: list[str]) -> None:
        self.sink = sink

    def execute(self, sql: str) -> None:
        self.sink.append(sql)

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class FakeConnection:
    def __init__(self, sink: list[str], vendor: str = "postgresql") -> None:
        self.vendor = vendor
        self._sink = sink
        # NO_DB_ALIAS makes Postgres's own receiver bail, isolating ours here.
        self.alias = NO_DB_ALIAS

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._sink)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(db_timeout.STATEMENT_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", ["uvicorn", "app.asgi:application"])


@pytest.fixture
def installed() -> Iterator[None]:
    db_timeout.install()
    yield
    db_timeout.uninstall()


def fire(sink: list[str], vendor: str = "postgresql") -> list[str]:
    """Send the real signal, so a receiver signature mismatch fails the test."""
    connection_created.send(sender=object(), connection=FakeConnection(sink, vendor))
    return sink


class TestIsMigrationProcess:
    @pytest.mark.parametrize("command", sorted(db_timeout.MIGRATION_COMMANDS))
    def test_migrations_run_without_a_ceiling(self, command: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["manage.py", command])
        assert db_timeout.is_migration_process() is True
        assert db_timeout.statement_timeout_ms() == 0

    @pytest.mark.parametrize(
        "argv",
        [
            ["manage.py", "runserver"],
            ["celery", "-A", "app", "worker"],
            ["uvicorn", "app.asgi:application"],
            ["gunicorn", "app.asgi:application"],
            ["manage.py"],
        ],
    )
    def test_web_and_workers_are_capped(self, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", argv)
        assert db_timeout.is_migration_process() is False
        assert db_timeout.statement_timeout_ms() == db_timeout.DB_STATEMENT_TIMEOUT_MS


class TestStatementTimeoutMs:
    def test_the_env_var_is_read_per_call_not_at_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A module-level int() would bind the value before a service's own env loading runs.
        monkeypatch.setenv(db_timeout.STATEMENT_TIMEOUT_ENV, "5000")
        assert db_timeout.statement_timeout_ms() == 5000

    def test_an_explicit_default_overrides_the_fleet_default(self) -> None:
        assert db_timeout.statement_timeout_ms(default=9000) == 9000

    def test_a_non_numeric_value_is_a_configuration_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(db_timeout.STATEMENT_TIMEOUT_ENV, "soon")
        with pytest.raises(ConfigurationError):
            db_timeout.statement_timeout_ms()


class TestReceiver:
    def test_sets_the_ceiling_on_new_connections(self, installed: None) -> None:
        assert fire([]) == [f"SET statement_timeout = {db_timeout.DB_STATEMENT_TIMEOUT_MS}"]

    def test_explicitly_clears_the_ceiling_for_migrations(
        self, installed: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Must actively reset, not skip: PgBouncer can leak a prior client's ceiling onto the pooled connection.
        monkeypatch.setattr(sys, "argv", ["manage.py", "migrate"])
        assert fire([]) == ["SET statement_timeout = 0"]

    def test_ignores_non_postgres_backends(self, installed: None) -> None:
        # Direct call, not via signal: other apps' receivers expect fuller connections.
        sink: list[str] = []
        db_timeout._apply_statement_timeout(connection=FakeConnection(sink, vendor="sqlite"))
        assert sink == []

    def test_the_private_name_stays_callable_for_the_repos_direct_call_tests(self) -> None:
        sink: list[str] = []
        db_timeout._apply_statement_timeout(connection=FakeConnection(sink))
        assert sink == [f"SET statement_timeout = {db_timeout.DB_STATEMENT_TIMEOUT_MS}"]


class TestInstall:
    def test_nothing_is_registered_by_importing_the_module(self) -> None:
        assert fire([]) == []

    def test_a_pinned_timeout_wins_over_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(db_timeout.STATEMENT_TIMEOUT_ENV, "5000")
        db_timeout.install(timeout_ms=1234)
        try:
            assert fire([]) == ["SET statement_timeout = 1234"]
        finally:
            db_timeout.uninstall()

    def test_a_repeat_install_registers_one_receiver(self) -> None:
        db_timeout.install()
        db_timeout.install()
        try:
            assert len(fire([])) == 1
        finally:
            db_timeout.uninstall()

    def test_install_returns_the_receiver(self) -> None:
        receiver = db_timeout.install()
        try:
            assert callable(receiver)
        finally:
            db_timeout.uninstall()


class TestTimeoutInvariant:
    def test_a_clearing_request_ceiling_passes(self) -> None:
        assert assert_timeout_invariant(request_timeout_seconds=60, statement_timeout_ms=30000) is None

    def test_a_request_ceiling_below_the_statement_timeout_fails(self) -> None:
        with pytest.raises(AssertionError, match="must exceed"):
            assert_timeout_invariant(request_timeout_seconds=30, statement_timeout_ms=30000)
