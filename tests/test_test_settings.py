"""Tests for the two-phase test-settings recipe: env application, base import, and the override dict."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from netix_backend.django import test_settings, testing
from netix_backend.django.test_settings import (
    DisableMigrations,
    EnvoySpec,
    apply_test_env,
    load_base_settings,
    test_overrides,
)

SQLITE = "django.db.backends.sqlite3"
LOCMEM = "django.core.cache.backends.locmem.LocMemCache"


@pytest.fixture
def clean_env() -> Iterator[None]:
    """apply_test_env writes straight to os.environ, so the whole mapping is snapshotted and put back."""
    original = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


@pytest.fixture
def base_module() -> Iterator[ModuleType]:
    """A stand-in for `app.settings`: the only attribute test_overrides reads from it is MIDDLEWARE."""
    module = ModuleType("netix_fake_settings")
    module.MIDDLEWARE = [  # type: ignore[attr-defined]
        "django.middleware.common.CommonMiddleware",
        testing.ENVOY_MIDDLEWARE_PATH,
        "app.middleware.RequestLog",
    ]
    module.ALLOWED_HOSTS = ["from-the-base"]  # type: ignore[attr-defined]
    module.lowercase_helper = "ignored"  # type: ignore[attr-defined]
    sys.modules["netix_fake_settings"] = module
    try:
        yield module
    finally:
        del sys.modules["netix_fake_settings"]


class TestApplyTestEnv:
    def test_the_baseline_is_setdefaulted(self, clean_env: None) -> None:
        os.environ.pop("POSTGRES_NAME", None)
        applied = apply_test_env()
        assert os.environ["POSTGRES_NAME"] == "test"
        assert applied["POSTGRES_NAME"] == "test"

    def test_csrf_trusted_origins_joined_the_baseline(self) -> None:
        # Six repos set it with five different values and none of them could inherit it from the library.
        assert testing.TEST_ENV_DEFAULTS["CSRF_TRUSTED_ORIGINS"] == "http://localhost:8000"

    def test_an_exported_value_wins_and_is_reported_back(self, clean_env: None) -> None:
        os.environ["REDIS_HOST"] = "redis-prod.internal"
        applied = apply_test_env()
        assert os.environ["REDIS_HOST"] == "redis-prod.internal"
        assert applied["REDIS_HOST"] == "redis-prod.internal"

    def test_keyword_overrides_extend_and_replace_the_baseline(self, clean_env: None) -> None:
        os.environ.pop("CSRF_TRUSTED_ORIGINS", None)
        applied = apply_test_env(CSRF_TRUSTED_ORIGINS="http://testserver", USER_SVC_URL="http://user")
        assert os.environ["CSRF_TRUSTED_ORIGINS"] == "http://testserver"
        assert applied["USER_SVC_URL"] == "http://user"

    def test_force_beats_an_exported_value(self, clean_env: None) -> None:
        # Hazard 2: cafm-backend assigns these, because an unreachable REDIS_HOST hangs Celery's .delay().
        os.environ["REDIS_HOST"] = "redis-prod.internal"
        applied = apply_test_env(force={"REDIS_HOST": "localhost", "SENTINEL_SERVICE": "mymaster"})
        assert os.environ["REDIS_HOST"] == "localhost"
        assert applied["REDIS_HOST"] == "localhost"
        assert applied["SENTINEL_SERVICE"] == "mymaster"

    def test_exclude_leaves_a_key_unset(self, clean_env: None) -> None:
        # Hazard 1: user-management derives MFA_SECRET_ENCRYPTION_KEY from DJANGO_SECRET and must own the default.
        os.environ.pop("DJANGO_SECRET", None)
        applied = apply_test_env(exclude=("DJANGO_SECRET",))
        assert "DJANGO_SECRET" not in os.environ
        assert "DJANGO_SECRET" not in applied
        assert applied["POSTGRES_NAME"] == "test"

    def test_exclude_also_beats_force(self, clean_env: None) -> None:
        os.environ.pop("REDIS_HOST", None)
        applied = apply_test_env(force={"REDIS_HOST": "localhost"}, exclude=("REDIS_HOST",))
        assert "REDIS_HOST" not in os.environ
        assert "REDIS_HOST" not in applied

    def test_session_customer_filter_stays_overridable(self, clean_env: None) -> None:
        # Hazard 3: redirect-service exercises real org scoping, so the FALSE baseline has to be invertible.
        os.environ.pop("SESSION_CUSTOMER_FILTER", None)
        applied = apply_test_env(SESSION_CUSTOMER_FILTER="TRUE")
        assert applied["SESSION_CUSTOMER_FILTER"] == "TRUE"


class TestLoadBaseSettings:
    def test_uppercase_names_are_copied_and_the_module_returned(self, base_module: ModuleType) -> None:
        namespace: dict[str, Any] = {}
        returned = load_base_settings(namespace, "netix_fake_settings")
        assert returned is base_module
        assert namespace["ALLOWED_HOSTS"] == ["from-the-base"]
        assert "lowercase_helper" not in namespace

    def test_an_existing_name_is_overwritten_by_default(self, base_module: ModuleType) -> None:
        namespace: dict[str, Any] = {"ALLOWED_HOSTS": ["already-here"]}
        load_base_settings(namespace, "netix_fake_settings")
        assert namespace["ALLOWED_HOSTS"] == ["from-the-base"]

    def test_skip_existing_reproduces_asset_services_guard(self, base_module: ModuleType) -> None:
        namespace: dict[str, Any] = {"ALLOWED_HOSTS": ["already-here"]}
        load_base_settings(namespace, "netix_fake_settings", skip_existing=True)
        assert namespace["ALLOWED_HOSTS"] == ["already-here"]
        assert namespace["MIDDLEWARE"] == base_module.MIDDLEWARE


class TestDatabases:
    def test_the_default_is_sqlite_in_memory(self) -> None:
        assert test_overrides()["DATABASES"] == {"default": {"ENGINE": SQLITE, "NAME": ":memory:"}}

    def test_a_path_is_taken_as_the_sqlite_name(self, tmp_path: Path) -> None:
        name = tmp_path / "test.sqlite3"
        assert test_overrides(database=name)["DATABASES"]["default"]["NAME"] == name

    def test_the_engine_is_choosable_for_the_gis_repos(self) -> None:
        engine = "django.contrib.gis.db.backends.spatialite"
        assert test_overrides(engine=engine)["DATABASES"]["default"]["ENGINE"] == engine

    def test_a_mapping_is_taken_verbatim(self) -> None:
        config = {"ENGINE": "django.contrib.gis.db.backends.postgis", "NAME": "asset", "HOST": "postgis"}
        assert test_overrides(database=config)["DATABASES"]["default"] == config

    def test_an_alias_clones_the_default(self) -> None:
        databases = test_overrides(aliases=("readonly",))["DATABASES"]
        assert databases["readonly"] == databases["default"]
        assert databases["readonly"] is not databases["default"]

    def test_alias_mirror_pins_the_replica_to_the_primary(self) -> None:
        databases = test_overrides(database="test.sqlite3", aliases=("readonly",), alias_mirror=True)["DATABASES"]
        assert databases["readonly"]["TEST"] == {"MIRROR": "default"}
        assert "TEST" not in databases["default"]

    def test_atomic_requests_reaches_every_alias(self) -> None:
        databases = test_overrides(aliases=("readonly",), atomic_requests=False)["DATABASES"]
        assert databases["default"]["ATOMIC_REQUESTS"] is False
        assert databases["readonly"]["ATOMIC_REQUESTS"] is False

    def test_atomic_requests_is_absent_unless_asked_for(self) -> None:
        assert "ATOMIC_REQUESTS" not in test_overrides()["DATABASES"]["default"]

    def test_routers_are_cleared_by_default(self) -> None:
        assert test_overrides()["DATABASE_ROUTERS"] == []

    def test_a_repo_that_tests_its_router_keeps_it(self) -> None:
        assert "DATABASE_ROUTERS" not in test_overrides(clear_routers=False)


class TestCachesAndSession:
    def test_locmem_without_a_location_by_default(self) -> None:
        assert test_overrides()["CACHES"] == {"default": {"BACKEND": LOCMEM}}

    def test_the_location_keeps_parallel_workers_apart(self) -> None:
        assert test_overrides(cache_location="tag-tests")["CACHES"]["default"]["LOCATION"] == "tag-tests"

    def test_no_session_engine_unless_asked_for(self) -> None:
        assert "SESSION_ENGINE" not in test_overrides()

    def test_the_cache_session_backend(self) -> None:
        assert test_overrides(session_backend="cache")["SESSION_ENGINE"] == "django.contrib.sessions.backends.cache"

    def test_the_signed_cookie_session_backend(self) -> None:
        engine = test_overrides(session_backend="signed_cookies")["SESSION_ENGINE"]
        assert engine == "django.contrib.sessions.backends.signed_cookies"


class TestSpeedKnobs:
    def test_every_knob_is_off_by_default(self) -> None:
        assert set(test_overrides()) == {"DATABASES", "CACHES", "DATABASE_ROUTERS"}

    def test_fast_hashers(self) -> None:
        assert test_overrides(fast_hashers=True)["PASSWORD_HASHERS"] == [
            "django.contrib.auth.hashers.MD5PasswordHasher"
        ]

    def test_disable_migrations_everywhere(self) -> None:
        modules = test_overrides(disable_migrations=True)["MIGRATION_MODULES"]
        assert isinstance(modules, DisableMigrations)

    def test_disable_migrations_for_named_labels(self) -> None:
        assert test_overrides(disable_migrations=["tag"])["MIGRATION_MODULES"] == {"tag": None}

    def test_an_empty_label_list_leaves_migrations_alone(self) -> None:
        assert "MIGRATION_MODULES" not in test_overrides(disable_migrations=[])

    def test_static_root_true_takes_a_throwaway_directory(self) -> None:
        static_root = test_overrides(static_root=True)["STATIC_ROOT"]
        assert os.path.isdir(static_root)
        assert os.listdir(static_root) == []

    def test_static_root_as_a_path_is_created(self, tmp_path: Path) -> None:
        target = str(tmp_path / "staticfiles")
        assert test_overrides(static_root=target)["STATIC_ROOT"] == target
        assert os.path.isdir(target)

    def test_static_root_is_idempotent_when_the_path_exists(self, tmp_path: Path) -> None:
        target = str(tmp_path / "staticfiles")
        test_overrides(static_root=target)
        assert test_overrides(static_root=target)["STATIC_ROOT"] == target

    def test_email_locmem(self) -> None:
        assert test_overrides(email_locmem=True)["EMAIL_BACKEND"] == "django.core.mail.backends.locmem.EmailBackend"

    def test_celery_eager(self) -> None:
        overrides = test_overrides(celery_eager=True)
        assert overrides["CELERY_TASK_ALWAYS_EAGER"] is True
        assert overrides["CELERY_TASK_EAGER_PROPAGATES"] is False
        assert overrides["CELERY_BROKER_URL"] == "memory://"
        assert overrides["CELERY_RESULT_BACKEND"] == "cache+memory://"

    def test_the_celery_block_is_copied_per_call(self) -> None:
        test_overrides(celery_eager=True)["CELERY_BROKER_URL"] = "stowaway"
        assert test_settings.CELERY_EAGER_SETTINGS["CELERY_BROKER_URL"] == "memory://"

    def test_quiet_db_logging(self) -> None:
        logging_config = test_overrides(quiet_db_logging=True)["LOGGING"]
        assert logging_config["handlers"]["console"]["class"] == "logging.NullHandler"
        assert logging_config["loggers"]["django.db.backends"]["level"] == "ERROR"

    def test_the_logging_block_is_rebuilt_per_call(self) -> None:
        first = test_overrides(quiet_db_logging=True)["LOGGING"]
        first["loggers"]["django.db.backends"]["level"] = "DEBUG"
        assert test_overrides(quiet_db_logging=True)["LOGGING"]["loggers"]["django.db.backends"]["level"] == "ERROR"


class TestDisableMigrationsMapping:
    def test_every_app_is_reported_as_present(self) -> None:
        assert "any_app_label" in DisableMigrations()

    def test_every_lookup_returns_none(self) -> None:
        assert DisableMigrations()["any_app_label"] is None


class TestEnvoyOverrides:
    def test_no_envoy_keys_unless_a_spec_is_given(self, base_module: ModuleType) -> None:
        assert "NETIX_TEST_ENVOY_IDENTITY" not in test_overrides(base_module)

    def test_the_resolver_seam_is_the_default_mode(self, base_module: ModuleType) -> None:
        overrides = test_overrides(base_module, envoy=EnvoySpec(permissions=["tag-view"]))
        assert overrides["MIDDLEWARE"][1] == testing.TEST_MIDDLEWARE_PATH
        assert overrides["NETIX_TEST_ENVOY_IDENTITY"]["permissions"] == ["tag-view"]
        assert overrides["NETIX_TEST_ENVOY_BEARER"] == testing.DEFAULT_TEST_BEARER

    def test_the_explicit_middleware_mode(self, base_module: ModuleType) -> None:
        overrides = test_overrides(base_module, envoy=EnvoySpec(mode="explicit"))
        assert overrides["MIDDLEWARE"][1] == testing.EXPLICIT_MIDDLEWARE_PATH

    def test_mode_none_keeps_the_production_middleware(self, base_module: ModuleType) -> None:
        # report-engine mirrors prod: the real AuthorizationMiddleware stays and identity arrives per fixture.
        overrides = test_overrides(base_module, envoy=EnvoySpec(mode="none"))
        assert "MIDDLEWARE" not in overrides
        assert overrides["NETIX_TEST_ENVOY_IDENTITY"]["organization"] == 0

    def test_a_repo_local_middleware_wins(self, base_module: ModuleType) -> None:
        spec = EnvoySpec(middleware="app.test_auth_middleware.TestAuthorizationMiddleware")
        overrides = test_overrides(base_module, envoy=spec)
        assert overrides["MIDDLEWARE"][1] == "app.test_auth_middleware.TestAuthorizationMiddleware"

    def test_a_standalone_settings_module_gets_identity_without_a_middleware_swap(self) -> None:
        overrides = test_overrides(envoy=EnvoySpec(permissions=["ai-report-view"]))
        assert "MIDDLEWARE" not in overrides
        assert overrides["NETIX_TEST_ENVOY_IDENTITY"]["permissions"] == ["ai-report-view"]

    def test_the_username_names_both_the_identity_and_the_bearer(self, base_module: ModuleType) -> None:
        overrides = test_overrides(base_module, envoy=EnvoySpec(username="tag-service-test-platform"))
        assert overrides["NETIX_TEST_ENVOY_IDENTITY"]["username"] == "tag-service-test-platform"
        assert overrides["NETIX_TEST_ENVOY_BEARER"] == "Bearer tag-service-test-platform"

    def test_an_explicit_bearer_wins(self, base_module: ModuleType) -> None:
        spec = EnvoySpec(username="stormbreaker-test-platform", bearer="api stormbreaker-key")
        assert test_overrides(base_module, envoy=spec)["NETIX_TEST_ENVOY_BEARER"] == "api stormbreaker-key"

    def test_a_verbatim_identity_bypasses_the_platform_superset(self, base_module: ModuleType) -> None:
        # cafm-backend and visualization-backend carry their own hand-written identity dicts.
        identity = {"organization": 0, "is_superuser": True, "permissions": ["quotation-delete"]}
        overrides = test_overrides(base_module, envoy=EnvoySpec(identity=identity))
        assert overrides["NETIX_TEST_ENVOY_IDENTITY"] == identity
        assert overrides["NETIX_TEST_ENVOY_IDENTITY"] is not identity


class TestRepoCollapse:
    """Each call reproduces one repo's current effective override block, key for key."""

    def test_backend_template_and_gateway_service(self) -> None:
        overrides = test_overrides(static_root=True)
        static_root = overrides.pop("STATIC_ROOT")
        assert overrides == {
            "DATABASES": {"default": {"ENGINE": SQLITE, "NAME": ":memory:"}},
            "CACHES": {"default": {"BACKEND": LOCMEM}},
            # The repo declares no router, so Django's own default is already [].
            "DATABASE_ROUTERS": [],
        }
        assert os.path.isdir(static_root)

    def test_backend_templates_session_customer_filter_line_is_now_redundant(self) -> None:
        # app/settings.py reads os.environ["SESSION_CUSTOMER_FILTER"] == "TRUE", which the baseline already answers.
        assert testing.TEST_ENV_DEFAULTS["SESSION_CUSTOMER_FILTER"] == "FALSE"

    def test_tag_service(self, base_module: ModuleType, tmp_path: Path) -> None:
        name = tmp_path / "test.sqlite3"
        overrides = test_overrides(
            base_module,
            database=name,
            aliases=("readonly",),
            cache_location="tag-tests",
            session_backend="signed_cookies",
            disable_migrations=["tag"],
            envoy=EnvoySpec(
                permissions=["tag-view", "tag-edit", "tag-delete", "control-tag-write", "tagging-rule-configure"],
                username="tag-service-test-platform",
            ),
        )
        assert overrides == {
            "DATABASES": {
                "default": {"ENGINE": SQLITE, "NAME": name},
                "readonly": {"ENGINE": SQLITE, "NAME": name},
            },
            "DATABASE_ROUTERS": [],
            "CACHES": {"default": {"BACKEND": LOCMEM, "LOCATION": "tag-tests"}},
            "SESSION_ENGINE": "django.contrib.sessions.backends.signed_cookies",
            "MIGRATION_MODULES": {"tag": None},
            "NETIX_TEST_ENVOY_IDENTITY": {
                "username": "tag-service-test-platform",
                "user_id": 0,
                "organization": 0,
                "is_superuser": True,
                "is_platform_internal": True,
                "user_type": "service",
                "permissions": [
                    "tag-view",
                    "tag-edit",
                    "tag-delete",
                    "control-tag-write",
                    "tagging-rule-configure",
                ],
                "groups": [],
                "feature_flags": [],
            },
            "NETIX_TEST_ENVOY_BEARER": "Bearer tag-service-test-platform",
            "MIDDLEWARE": [
                "django.middleware.common.CommonMiddleware",
                testing.TEST_MIDDLEWARE_PATH,
                "app.middleware.RequestLog",
            ],
        }

    def test_stormbreaker_backend(self, base_module: ModuleType, tmp_path: Path) -> None:
        static_root = str(tmp_path / "stormbreaker-staticfiles")
        overrides = test_overrides(
            base_module,
            aliases=("readonly",),
            fast_hashers=True,
            static_root=static_root,
            envoy=EnvoySpec(
                username="stormbreaker-test-platform",
                middleware="app.test_auth_middleware.TestAuthorizationMiddleware",
            ),
        )
        assert overrides == {
            "DATABASES": {
                "default": {"ENGINE": SQLITE, "NAME": ":memory:"},
                "readonly": {"ENGINE": SQLITE, "NAME": ":memory:"},
            },
            "DATABASE_ROUTERS": [],
            "CACHES": {"default": {"BACKEND": LOCMEM}},
            "PASSWORD_HASHERS": ["django.contrib.auth.hashers.MD5PasswordHasher"],
            "STATIC_ROOT": static_root,
            "NETIX_TEST_ENVOY_IDENTITY": {
                "username": "stormbreaker-test-platform",
                "user_id": 0,
                "organization": 0,
                "is_superuser": True,
                "is_platform_internal": True,
                "user_type": "service",
                "permissions": [],
                "groups": [],
                "feature_flags": [],
            },
            "NETIX_TEST_ENVOY_BEARER": "Bearer stormbreaker-test-platform",
            "MIDDLEWARE": [
                "django.middleware.common.CommonMiddleware",
                "app.test_auth_middleware.TestAuthorizationMiddleware",
                "app.middleware.RequestLog",
            ],
        }
        assert os.path.isdir(static_root)


class TestLegacyApiStillWorks:
    def test_netix_test_settings_still_returns_the_baseline(self) -> None:
        assert testing.netix_test_settings()["POSTGRES_NAME"] == "test"

    def test_sqlite_databases_still_returns_the_v1_shape(self) -> None:
        assert testing.sqlite_databases() == {"default": {"ENGINE": SQLITE, "NAME": ":memory:"}}

    def test_sqlite_databases_still_accepts_a_name(self) -> None:
        assert testing.sqlite_databases("/tmp/db.sqlite3")["default"]["NAME"] == "/tmp/db.sqlite3"
