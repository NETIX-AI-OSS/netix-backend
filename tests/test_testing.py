"""Tests for the pytest plugin: env defaults and the two Envoy scoping fixtures."""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest

import netix_backend
from netix_backend.django import testing

# The pytest11 entry point imports these before coverage starts, so re-execute them under measurement.
importlib.reload(netix_backend)
importlib.reload(testing)


class FakeQuerySet:
    """Records the queryset calls the real EnvoyQueryFilter makes, so no database is needed."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> FakeQuerySet:
        self.calls.append((name, args, kwargs))
        return self

    def all(self) -> FakeQuerySet:
        return self._record("all")

    def none(self) -> FakeQuerySet:
        return self._record("none")

    def filter(self, *args: Any, **kwargs: Any) -> FakeQuerySet:
        return self._record("filter", *args, **kwargs)

    def order_by(self, *args: Any) -> FakeQuerySet:
        return self._record("order_by", *args)


class FakeModel:
    objects = FakeQuerySet()


class FakeRequest:
    def __init__(self, organization: int) -> None:
        self.envoy = {"organization": organization}


@pytest.fixture
def fake_model() -> type[FakeModel]:
    FakeModel.objects = FakeQuerySet()
    return FakeModel


def call_names(queryset: FakeQuerySet) -> list[str]:
    return [call[0] for call in queryset.calls]


def envoy_filter() -> Any:
    from envoy_pyauth.utils import EnvoyQueryFilter

    return EnvoyQueryFilter


class TestModuleScopeIsImportLight:
    def test_pytest_is_the_only_module_bound_at_module_scope(self) -> None:
        bound = {name for name, value in vars(testing).items() if isinstance(value, types.ModuleType)}
        assert bound == {"pytest"}

    def test_plugin_is_loaded_without_a_conftest(self, pytestconfig: pytest.Config) -> None:
        assert pytestconfig.pluginmanager.hasplugin("netix_backend")
        assert sys.modules["netix_backend.django.testing"] is testing


class TestNetixTestSettings:
    def test_returns_the_shared_env_defaults(self) -> None:
        env = testing.netix_test_settings()
        assert env["DJANGO_SECRET"] == "test-secret-key-not-for-production"
        assert env["SESSION_CUSTOMER_FILTER"] == "FALSE"
        assert set(env) == set(testing.TEST_ENV_DEFAULTS)

    def test_extra_env_is_merged_and_wins(self) -> None:
        env = testing.netix_test_settings(extra_env={"REDIS_DB": "3", "USER_SVC_URL": "http://user"})
        assert env["REDIS_DB"] == "3"
        assert env["USER_SVC_URL"] == "http://user"

    def test_the_caller_cannot_mutate_the_defaults(self) -> None:
        testing.netix_test_settings()["REDIS_DB"] = "9"
        assert testing.TEST_ENV_DEFAULTS["REDIS_DB"] == "0"


class TestSettingsConstants:
    def test_sqlite_memory_databases(self) -> None:
        assert testing.SQLITE_MEMORY_DATABASES["default"]["NAME"] == ":memory:"

    def test_locmem_caches(self) -> None:
        backend = testing.LOCMEM_CACHES["default"]["BACKEND"]
        assert backend == "django.core.cache.backends.locmem.LocMemCache"

    def test_sqlite_databases_defaults_to_memory(self) -> None:
        assert testing.sqlite_databases() == testing.SQLITE_MEMORY_DATABASES

    def test_sqlite_databases_accepts_a_path(self) -> None:
        assert testing.sqlite_databases("/tmp/db.sqlite3")["default"]["NAME"] == "/tmp/db.sqlite3"


class TestUnscopedEnvoy:
    def test_get_queryset_returns_every_row(self, unscoped_envoy: None, fake_model: type[FakeModel]) -> None:
        result = envoy_filter().get_queryset(None, fake_model, True)
        assert call_names(fake_model.objects) == ["all"]
        assert result is fake_model.objects

    def test_get_queryset_tolerates_the_real_signature(self, unscoped_envoy: None, fake_model: type[FakeModel]) -> None:
        envoy_filter().get_queryset(None, fake_model, True, "organization_id", delete_filter=False)
        assert call_names(fake_model.objects) == ["all"]

    def test_filter_queryset_is_the_identity(self, unscoped_envoy: None) -> None:
        queryset = FakeQuerySet()
        assert envoy_filter().filter_queryset(None, queryset, True, include_shared=True) is queryset
        assert queryset.calls == []

    def test_the_patch_is_undone_afterwards(self) -> None:
        assert isinstance(envoy_filter().__dict__["get_queryset"], classmethod)


class TestExplicitEnvoyIdentity:
    def test_default_identity_delegates_to_the_real_implementation(
        self, explicit_envoy_identity: None, fake_model: type[FakeModel]
    ) -> None:
        envoy_filter().get_queryset(None, fake_model, True)
        # Organization 0 is the platform view, so the real filter runs its unscoped branch.
        assert call_names(fake_model.objects) == ["filter", "order_by"]

    def test_an_existing_request_is_left_alone(
        self, explicit_envoy_identity: None, fake_model: type[FakeModel]
    ) -> None:
        envoy_filter().get_queryset(FakeRequest(7), fake_model, True)
        name, args, _ = fake_model.objects.calls[0]
        assert name == "filter"
        assert "organization_id__in" in str(args[0])

    def test_filter_queryset_also_delegates(self, explicit_envoy_identity: None) -> None:
        queryset = FakeQuerySet()
        envoy_filter().filter_queryset(FakeRequest(7), queryset, True)
        assert call_names(queryset) == ["filter", "order_by"]

    def test_identity_fixture_is_overridable(self, netix_envoy_identity: dict[str, Any]) -> None:
        assert netix_envoy_identity == testing.DEFAULT_ENVOY_IDENTITY
        assert netix_envoy_identity is not testing.DEFAULT_ENVOY_IDENTITY


class TestScopedEnvoy:
    def test_scopes_to_the_supplied_organization(self, fake_model: type[FakeModel]) -> None:
        with testing.scoped_envoy({"organization": 7}):
            envoy_filter().get_queryset(None, fake_model, True)
        name, args, _ = fake_model.objects.calls[0]
        assert name == "filter"
        assert "7" in str(args[0])

    def test_defaults_to_the_shared_identity(self, fake_model: type[FakeModel]) -> None:
        with testing.scoped_envoy():
            envoy_filter().get_queryset(None, fake_model, True)
        assert call_names(fake_model.objects) == ["filter", "order_by"]

    def test_restores_the_original_classmethods(self) -> None:
        with testing.scoped_envoy():
            assert not isinstance(envoy_filter().__dict__["get_queryset"], classmethod)
        assert isinstance(envoy_filter().__dict__["get_queryset"], classmethod)


class TestPatchHelpers:
    def test_unscoped_helper_can_be_used_from_a_repo_conftest(
        self, monkeypatch: pytest.MonkeyPatch, fake_model: type[FakeModel]
    ) -> None:
        testing.patch_envoy_unscoped(monkeypatch)
        envoy_filter().get_queryset(None, fake_model, True)
        assert call_names(fake_model.objects) == ["all"]

    def test_identity_helper_accepts_an_explicit_identity(
        self, monkeypatch: pytest.MonkeyPatch, fake_model: type[FakeModel]
    ) -> None:
        testing.patch_envoy_identity(monkeypatch, {"organization": 12})
        envoy_filter().get_queryset(None, fake_model, True)
        name, args, _ = fake_model.objects.calls[0]
        assert name == "filter"
        assert "12" in str(args[0])
