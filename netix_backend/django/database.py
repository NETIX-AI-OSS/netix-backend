"""Re-export of :mod:`netix_backend.database` for settings that expect the factory under the django subpackage."""

from netix_backend.database import (
    OMIT,
    POSTGIS_ENGINE,
    POSTGRES_ENGINE,
    REQUIRED,
    FromEnv,
    Value,
    postgres_database,
    replica_of,
)

# The implementation is Django-free on purpose: settings.py builds DATABASES before Django is configured.
__all__ = (
    "OMIT",
    "POSTGIS_ENGINE",
    "POSTGRES_ENGINE",
    "REQUIRED",
    "FromEnv",
    "Value",
    "postgres_database",
    "replica_of",
)
