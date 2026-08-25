"""The DATABASES factory: the fleet's per-repo aliases reproduced as literal dicts, plus the OMIT/FromEnv contracts."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from netix_backend.database import (
    OMIT,
    POSTGIS_ENGINE,
    POSTGRES_ENGINE,
    REQUIRED,
    FromEnv,
    postgres_database,
    replica_of,
)

DB_ENV = {
    "POSTGRES_NAME": "netix",
    "POSTGRES_USER": "netix_user",
    "POSTGRES_PASSWORD": "hunter2",
    "POSTGRES_HOST": "db.svc",
    "POSTGRES_PORT": "5432",
}

# Cleared so every default-bearing FromEnv in the table below exercises its default rather than a stray value.
OPTIONAL_ENV = (
    "POSTGRES_CONN_MAX_AGE",
    "CONN_MAX_AGE",
    "POSTGRES_CONNECT_TIMEOUT",
    "POSTGRES_READ_HOST",
    "POSTGRES_READ_PORT",
    "POSTGRES_READONLY_HOST",
    "POSTGRES_READONLY_PORT",
    "POSTGRES_READ_REPLICA_HOST",
    "POSTGRES_READ_REPLICA_PORT",
    "DJANGO_DB_ENGINE",
)

IMPORT_GUARD = (
    "import sys; import netix_backend.database, netix_backend.django.database; "
    "print(netix_backend.django.database.postgres_database is netix_backend.database.postgres_database); "
    "assert not [m for m in sys.modules if m.split('.')[0] == 'django'], sorted(sys.modules)"
)


@pytest.fixture(autouse=True)
def db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in DB_ENV.items():
        monkeypatch.setenv(name, value)
    for name in OPTIONAL_ENV:
        monkeypatch.delenv(name, raising=False)


class TestPerRepoAliases:
    """Each expected dict is a literal copy of what the named settings.py binds today."""

    def test_gateway_service_license_server_report_engine_update_service(self) -> None:
        # Four repos plus backend-template produce this exact dict: no CONN_HEALTH_CHECKS, no OPTIONS.
        assert postgres_database(conn_max_age=0) == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "db.svc",
            "PORT": "5432",
            "CONN_MAX_AGE": 0,
            "ATOMIC_REQUESTS": False,
            "DISABLE_SERVER_SIDE_CURSORS": True,
        }

    def test_ml_engine(self) -> None:
        assert postgres_database(conn_max_age=0, conn_health_checks=True) == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "db.svc",
            "PORT": "5432",
            "CONN_MAX_AGE": 0,
            "CONN_HEALTH_CHECKS": True,
            "ATOMIC_REQUESTS": False,
            "DISABLE_SERVER_SIDE_CURSORS": True,
        }

    def test_notification_service_and_static_service_omit_conn_max_age(self) -> None:
        assert postgres_database(conn_max_age=OMIT) == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "db.svc",
            "PORT": "5432",
            "ATOMIC_REQUESTS": False,
            "DISABLE_SERVER_SIDE_CURSORS": True,
        }

    def test_asset_service_default_and_conditional_read_replica(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_CONN_MAX_AGE", "60")
        conn_max_age = int(os.environ.get("POSTGRES_CONN_MAX_AGE", 60))
        default = postgres_database(
            engine=POSTGIS_ENGINE,
            conn_max_age=conn_max_age,
            conn_health_checks=True,
            prepare_threshold=None,
        )
        assert default == {
            "ENGINE": "django.contrib.gis.db.backends.postgis",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "db.svc",
            "PORT": "5432",
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "ATOMIC_REQUESTS": False,
            "DISABLE_SERVER_SIDE_CURSORS": True,
            "OPTIONS": {"prepare_threshold": None},
        }
        replica = postgres_database(
            engine=default["ENGINE"],
            name=default["NAME"],
            user=default["USER"],
            password=default["PASSWORD"],
            host="replica.svc",
            port="5433",
            conn_max_age=conn_max_age,
            conn_health_checks=True,
            prepare_threshold=None,
            test={"MIRROR": "default"},
        )
        assert replica == {**default, "HOST": "replica.svc", "PORT": "5433", "TEST": {"MIRROR": "default"}}
        # H4: the two aliases must not share one OPTIONS object the way the hand-written _DB_OPTIONS did.
        assert replica["OPTIONS"] is not default["OPTIONS"]

    def test_cafm_backend_default_and_unconditional_replica(self) -> None:
        default = postgres_database(
            name=FromEnv("POSTGRES_NAME", "cafm"),
            user=FromEnv("POSTGRES_USER", "postgres"),
            password=FromEnv("POSTGRES_PASSWORD", "postgres"),
            host=FromEnv("POSTGRES_HOST", "historian.platform"),
            port=FromEnv("POSTGRES_PORT", "5432"),
            conn_max_age=FromEnv("POSTGRES_CONN_MAX_AGE", 0, cast=int),
            conn_health_checks=False,
            prepare_threshold=None,
        )
        assert default == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "db.svc",
            "PORT": "5432",
            "CONN_MAX_AGE": 0,
            "CONN_HEALTH_CHECKS": False,
            "ATOMIC_REQUESTS": False,
            "DISABLE_SERVER_SIDE_CURSORS": True,
            "OPTIONS": {"prepare_threshold": None},
        }
        assert replica_of(default, host="replica.platform", port="5433", test={"MIRROR": "default"}) == {
            **default,
            "HOST": "replica.platform",
            "PORT": "5433",
            "TEST": {"MIRROR": "default"},
        }

    def test_data_service_default_and_strict_readonly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_READ_HOST", "readonly.svc")
        monkeypatch.setenv("POSTGRES_READ_PORT", "5433")
        common = {"conn_max_age": 60, "conn_health_checks": True, "prepare_threshold": None}
        expected = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "db.svc",
            "PORT": "5432",
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "ATOMIC_REQUESTS": False,
            "DISABLE_SERVER_SIDE_CURSORS": True,
            "OPTIONS": {"prepare_threshold": None},
        }
        assert postgres_database(**common) == expected
        readonly = postgres_database(host=FromEnv("POSTGRES_READ_HOST"), port=FromEnv("POSTGRES_READ_PORT"), **common)
        assert readonly == {**expected, "HOST": "readonly.svc", "PORT": "5433"}

    def test_redirect_service(self) -> None:
        assert postgres_database(
            host=FromEnv("POSTGRES_HOST", "historian.platform"),
            port=FromEnv("POSTGRES_PORT", "5432"),
            conn_max_age=60,
            conn_health_checks=True,
            prepare_threshold=None,
            connect_timeout=5,
        ) == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "db.svc",
            "PORT": "5432",
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
            "ATOMIC_REQUESTS": False,
            "DISABLE_SERVER_SIDE_CURSORS": True,
            "OPTIONS": {"prepare_threshold": None, "connect_timeout": 5},
        }

    def test_tag_service_default_and_readonly_without_atomic_requests(self) -> None:
        common = {
            "conn_max_age": FromEnv("CONN_MAX_AGE", "0", cast=int),
            "prepare_threshold": None,
            "connect_timeout": 5,
        }
        assert postgres_database(atomic_requests=True, **common) == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "db.svc",
            "PORT": "5432",
            "CONN_MAX_AGE": 0,
            "ATOMIC_REQUESTS": True,
            "DISABLE_SERVER_SIDE_CURSORS": True,
            "OPTIONS": {"prepare_threshold": None, "connect_timeout": 5},
        }
        # The readonly alias is the only one in the fleet that drops ATOMIC_REQUESTS while the default sets it.
        assert postgres_database(
            atomic_requests=OMIT,
            host=FromEnv("POSTGRES_READONLY_HOST", "historian.platform"),
            port=FromEnv("POSTGRES_READONLY_PORT", "5433"),
            **common,
        ) == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "historian.platform",
            "PORT": "5433",
            "CONN_MAX_AGE": 0,
            "DISABLE_SERVER_SIDE_CURSORS": True,
            "OPTIONS": {"prepare_threshold": None, "connect_timeout": 5},
        }

    def test_user_management_replica_inherits_everything_but_host_and_port(self) -> None:
        default = postgres_database(
            host=FromEnv("POSTGRES_HOST", "historian.platform"),
            port=FromEnv("POSTGRES_PORT", "5432"),
            conn_max_age=60,
            conn_health_checks=True,
            prepare_threshold=None,
            connect_timeout=5,
        )
        replica = replica_of(default, host="historian.platform", port="5433")
        assert replica == {**default, "HOST": "historian.platform", "PORT": "5433"}
        assert "TEST" not in replica

    def test_vision_ai_service_needs_three_omits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # H14: the only repo where the proposed defaults would *add* keys, so all three are dropped explicitly.
        for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT"):
            monkeypatch.delenv(name)
        assert postgres_database(
            atomic_requests=OMIT,
            disable_server_side_cursors=OMIT,
            user=FromEnv("POSTGRES_USER", "postgres"),
            password=FromEnv("POSTGRES_PASSWORD", ""),
            host=FromEnv("POSTGRES_HOST", "localhost"),
            port=FromEnv("POSTGRES_PORT", "5432"),
            conn_max_age=0,
            conn_health_checks=True,
        ) == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "postgres",
            "PASSWORD": "",
            "HOST": "localhost",
            "PORT": "5432",
            "CONN_MAX_AGE": 0,
            "CONN_HEALTH_CHECKS": True,
        }

    def test_visualization_backend_readonly_carries_a_test_name(self) -> None:
        readonly = postgres_database(
            host=FromEnv("POSTGRES_READONLY_HOST", "historian.platform"),
            port=FromEnv("POSTGRES_READONLY_PORT", "5433"),
            conn_max_age=0,
            conn_health_checks=True,
            prepare_threshold=None,
            test={"NAME": "postgres"},
        )
        assert readonly == {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": "netix",
            "USER": "netix_user",
            "PASSWORD": "hunter2",
            "HOST": "historian.platform",
            "PORT": "5433",
            "CONN_MAX_AGE": 0,
            "CONN_HEALTH_CHECKS": True,
            "ATOMIC_REQUESTS": False,
            "DISABLE_SERVER_SIDE_CURSORS": True,
            "OPTIONS": {"prepare_threshold": None},
            "TEST": {"NAME": "postgres"},
        }


class TestOptionsEmission:
    def test_no_options_key_at_all_by_default(self) -> None:
        # Invariants 1-2: nine services have no OPTIONS key today and adoption must not invent one.
        assert "OPTIONS" not in postgres_database()

    def test_prepare_threshold_none_is_not_the_same_as_omit(self) -> None:
        assert postgres_database(prepare_threshold=None)["OPTIONS"] == {"prepare_threshold": None}

    def test_prepare_threshold_can_carry_a_real_value(self) -> None:
        assert postgres_database(prepare_threshold=5)["OPTIONS"] == {"prepare_threshold": 5}

    def test_connect_timeout_alone_emits_options(self) -> None:
        assert postgres_database(connect_timeout=5)["OPTIONS"] == {"connect_timeout": 5}

    def test_connect_timeout_reads_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_CONNECT_TIMEOUT", "7")
        options = postgres_database(connect_timeout=FromEnv("POSTGRES_CONNECT_TIMEOUT", "5", cast=int))["OPTIONS"]
        assert options == {"connect_timeout": 7}

    def test_free_form_options_merge_last(self) -> None:
        entry = postgres_database(prepare_threshold=None, options={"sslmode": "require"})
        assert entry["OPTIONS"] == {"prepare_threshold": None, "sslmode": "require"}


class TestOmitAndExtras:
    def test_omit_drops_the_core_keys_too(self) -> None:
        assert set(postgres_database(engine=OMIT, name=OMIT, user=OMIT, password=OMIT, port=OMIT)) == {
            "HOST",
            "CONN_MAX_AGE",
            "ATOMIC_REQUESTS",
            "DISABLE_SERVER_SIDE_CURSORS",
        }

    def test_extra_keys_are_appended_verbatim(self) -> None:
        assert postgres_database(extra={"TIME_ZONE": "UTC"})["TIME_ZONE"] == "UTC"

    def test_key_order_pins_the_repr_and_ignores_missing_keys(self) -> None:
        # H1: Django reads DATABASES by key, so this exists only for a repo that snapshots the dict repr.
        entry = postgres_database(key_order=("NAME", "OPTIONS", "ENGINE"))
        assert list(entry)[:2] == ["NAME", "ENGINE"]

    def test_default_key_order_is_the_django_settings_order(self) -> None:
        assert list(postgres_database(conn_health_checks=True, prepare_threshold=None, test={})) == [
            "ENGINE",
            "NAME",
            "USER",
            "PASSWORD",
            "HOST",
            "PORT",
            "CONN_MAX_AGE",
            "CONN_HEALTH_CHECKS",
            "ATOMIC_REQUESTS",
            "DISABLE_SERVER_SIDE_CURSORS",
            "OPTIONS",
            "TEST",
        ]

    def test_the_sentinels_repr_as_their_names(self) -> None:
        assert repr(OMIT) == "OMIT"
        assert repr(REQUIRED) == "REQUIRED"


class TestFromEnv:
    def test_a_required_default_reproduces_os_environ_indexing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POSTGRES_NAME")
        with pytest.raises(KeyError, match="POSTGRES_NAME"):
            postgres_database()

    def test_a_default_is_used_when_the_variable_is_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("POSTGRES_HOST")
        assert FromEnv("POSTGRES_HOST", "historian.platform").resolve() == "historian.platform"

    def test_an_empty_variable_stays_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # H2: _lookup would map "" to None and start connecting to historian.platform instead of failing loudly.
        monkeypatch.setenv("POSTGRES_HOST", "")
        assert FromEnv("POSTGRES_HOST", "historian.platform").resolve() == ""
        assert postgres_database(host=FromEnv("POSTGRES_HOST", "historian.platform"))["HOST"] == ""

    def test_a_whitespace_variable_is_not_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_HOST", "  ")
        assert FromEnv("POSTGRES_HOST").resolve() == "  "

    def test_cast_runs_on_the_resolved_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONN_MAX_AGE", "45")
        assert FromEnv("CONN_MAX_AGE", "0", cast=int).resolve() == 45

    def test_cast_runs_on_the_default_too(self) -> None:
        assert FromEnv("CONN_MAX_AGE", "0", cast=int).resolve() == 0

    def test_a_non_string_default_survives(self) -> None:
        assert FromEnv("POSTGRES_CONN_MAX_AGE", 0, cast=int).resolve() == 0


class TestReplicaOf:
    def test_key_order_of_the_primary_is_preserved(self) -> None:
        primary = {"ENGINE": "e", "NAME": "n", "HOST": "h", "PORT": "p", "CONN_MAX_AGE": 0}
        replica = replica_of(primary, host="replica", port="5433")
        assert list(replica) == ["ENGINE", "NAME", "HOST", "PORT", "CONN_MAX_AGE"]
        assert replica["HOST"] == "replica"
        assert replica["PORT"] == "5433"

    def test_the_primary_is_not_mutated(self) -> None:
        primary = {"HOST": "h", "PORT": "p"}
        replica_of(primary, host="replica", port="5433")
        assert primary == {"HOST": "h", "PORT": "p"}

    def test_overrides_are_upper_cased(self) -> None:
        primary = {"HOST": "h", "PORT": "p", "CONN_MAX_AGE": 60}
        assert replica_of(primary, host="r", port="5433", conn_max_age=0)["CONN_MAX_AGE"] == 0

    def test_an_override_can_read_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("POSTGRES_READ_HOST", "readonly.svc")
        assert replica_of({"HOST": "h", "PORT": "p"}, host=FromEnv("POSTGRES_READ_HOST"), port="5433")["HOST"] == (
            "readonly.svc"
        )

    def test_omit_drops_an_inherited_key(self) -> None:
        primary = {"HOST": "h", "PORT": "p", "ATOMIC_REQUESTS": True}
        assert "ATOMIC_REQUESTS" not in replica_of(primary, host="r", port="5433", atomic_requests=OMIT)

    def test_test_is_omitted_unless_asked_for(self) -> None:
        assert "TEST" not in replica_of({"HOST": "h", "PORT": "p"}, host="r", port="5433")


class TestModuleSurface:
    def test_imports_without_django(self) -> None:
        # settings.py builds DATABASES before Django is configured, so neither import path may pull Django in.
        result = subprocess.run([sys.executable, "-c", IMPORT_GUARD], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "True"

    def test_the_django_subpackage_re_export_is_the_same_object(self) -> None:
        from netix_backend.django import database as shim

        assert shim.postgres_database is postgres_database
        assert shim.replica_of is replica_of
        assert shim.OMIT is OMIT
        assert shim.POSTGRES_ENGINE == POSTGRES_ENGINE
