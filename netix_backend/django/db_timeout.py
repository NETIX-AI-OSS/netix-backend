"""Per-connection Postgres ``statement_timeout``, since PgBouncer rejects it as a startup parameter."""

from __future__ import annotations

import sys
from collections.abc import Callable
from functools import partial
from typing import Any, Final

from django.db.backends.signals import connection_created

from netix_backend.env import env_int

__all__ = (
    "DB_STATEMENT_TIMEOUT_MS",
    "DISPATCH_UID",
    "MIGRATION_COMMANDS",
    "STATEMENT_TIMEOUT_ENV",
    "install",
    "is_migration_process",
    "statement_timeout_ms",
    "uninstall",
)

# The fleet-wide default ceiling; the effective value is resolved per call so a service's env loading runs first.
DB_STATEMENT_TIMEOUT_MS: Final[int] = 30000

STATEMENT_TIMEOUT_ENV: Final[str] = "DB_STATEMENT_TIMEOUT_MS"
DISPATCH_UID: Final[str] = "netix_backend.django.db_timeout"

# argv[1] is the management command: `manage.py migrate` -> "migrate".
MIGRATION_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "migrate",
        "makemigrations",
        "sqlmigrate",
        "showmigrations",
        "squashmigrations",
    }
)


def is_migration_process() -> bool:
    """Whether this process is a migration command, which must run unbounded."""
    return len(sys.argv) > 1 and sys.argv[1] in MIGRATION_COMMANDS


def statement_timeout_ms(default: int = DB_STATEMENT_TIMEOUT_MS) -> int:
    """The ceiling this process applies, in milliseconds; 0 means "no ceiling"."""
    if is_migration_process():
        return 0
    return env_int(STATEMENT_TIMEOUT_ENV, default)


def _apply_statement_timeout(connection: Any, *, timeout_ms: int | None = None, **kwargs: Any) -> None:
    # `sender` and `signal` arrive in kwargs; only the connection matters here.
    if connection.vendor != "postgresql":
        return
    ceiling = statement_timeout_ms() if timeout_ms is None else timeout_ms
    # Always SET explicitly (even 0): PgBouncer leaks a prior client's timeout onto a pooled connection.
    with connection.cursor() as cursor:
        # SET does not accept a bound parameter; the value is a validated int.
        cursor.execute(f"SET statement_timeout = {int(ceiling)}")


def install(*, timeout_ms: int | None = None, dispatch_uid: str = DISPATCH_UID) -> Callable[..., None]:
    """Register the ``connection_created`` receiver; call it from ``AppConfig.ready()`` or settings."""
    receiver: Callable[..., None] = _apply_statement_timeout
    if timeout_ms is not None:
        receiver = partial(_apply_statement_timeout, timeout_ms=timeout_ms)
    # weak=False keeps a pinned-timeout partial alive; the dispatch_uid makes a repeat install a no-op.
    connection_created.connect(receiver, dispatch_uid=dispatch_uid, weak=False)
    return receiver


def uninstall(*, dispatch_uid: str = DISPATCH_UID) -> None:
    """Disconnect what ``install()`` registered, so a test suite can isolate the signal."""
    connection_created.disconnect(dispatch_uid=dispatch_uid)
