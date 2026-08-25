# v1.1.0 — Django layer notes (branch `v11-django`)

Proposed CHANGELOG bullets and README sections for the six extractions in this branch. Nothing
here is applied to `CHANGELOG.md` / `README.md` — those files are owned by another agent.

## Proposed CHANGELOG bullets

### Added

- `netix_backend.cloning` (Django-free) — the org-prefixed machine-key contract seven repos carry a
  private copy of: `TEMPLATE_ORG_ID`, `ORG_KEY_PREFIX`, `PROVENANCE_FIELDS`, `org_prefix`,
  `base_key`, `org_key`, `key_owner`, `is_org_key`. A superset of `envoy_pyauth.cloning`, vendored
  rather than re-exported: envoy-pyauth **v2.0.0 ships no `cloning` module**, and the library's
  floor is `>=2.0.0`, so a re-export would `ImportError` on cafm-backend, data-service and
  ml-engine. Pairs with the existing `CloneProvenanceMixin`.
- `netix_backend.django.db_timeout` — the per-connection Postgres `statement_timeout` receiver
  (`DB_STATEMENT_TIMEOUT_MS`, `MIGRATION_COMMANDS`, `is_migration_process()`,
  `statement_timeout_ms()`, `install()`, `uninstall()`). PgBouncer rejects the option as a startup
  parameter and leaks a prior client's ceiling onto a pooled connection, so every new connection is
  `SET` explicitly — including `SET statement_timeout = 0` for migration commands. Registration is
  explicit via `install()` rather than an import side effect.
- `netix_backend.django.org_bootstrap` — `build_org_bootstrap_view(clone=…, teardown=…, error=…,
  name=…, catalog=…, base=…)`, `OrgBootstrapBaseView`, `OrgBootstrapSerializer`,
  `OrgTeardownSerializer`, the `ClonePrimitives` / `TeardownPrimitives` protocols and a shared
  `CloneError`. Each of the six services drops a ~70-line `views_bootstrap.py` for a three-line
  binding; asset-service subclasses the generated view to add its clone-map `GET`.
- `netix_backend.django.cache` — `redis_caches(...)` and `cache_ttls(...)` returning frozen
  `RedisCacheSettings` / `CacheTtls` dataclasses, replacing the `SENTINEL_ENABLED` if/else that 13
  `settings.py` copy verbatim. Emits the whole django_redis knob set
  (`DJANGO_REDIS_CONNECTION_FACTORY`, `DJANGO_REDIS_IGNORE_EXCEPTIONS`,
  `DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS`, `DJANGO_REDIS_LOGGER`) so tag-service's and asset-service's
  Sentry `ignore_logger()` pattern survives adoption.
- `netix_backend.django.testing` — `EnvoyTestAuthorizationMiddleware` and
  `ExplicitEnvoyIdentityMiddleware` (settings-driven via `NETIX_TEST_ENVOY_IDENTITY` /
  `NETIX_TEST_ENVOY_BEARER`), `swap_auth_middleware()`, `platform_test_identity()`,
  `DEFAULT_TEST_PLATFORM_IDENTITY`, `DEFAULT_TEST_BEARER`, and `assert_timeout_invariant()` — the
  `REQUEST_TIMEOUT_SECONDS > DB_STATEMENT_TIMEOUT_MS / 1000` assertion nine repos duplicate.
- `netix_backend.django.excel_aio.AsyncExcelViewSet` — the adrf export shape user-management's
  `BaseExcelViewSet` needs (`list` delegating through `async_to_sync(self.alist)`, plus `alist` and
  `aretrieve`, all three carrying the binary response override).

### Changed

- `netix_backend.django.excel` is now an **envoy-free core**: `ExcelExportMixin` (renderer,
  filename, pagination, styles), `ExcelExportViewSet` (no `schema`), `DocumentedExcelViewSet`
  (`XlsxExportAutoSchema` + the binary responses). The scoped variant moved to
  `netix_backend.django.excel_envoy.ScopedExcelViewSet`; **`BaseExcelViewSet` is an alias of it and
  stays importable from `netix_backend.django.excel`** via a module `__getattr__`, so no existing
  import or subclass breaks. Importing `netix_backend.django.excel` no longer pulls
  `envoy_pyauth`.
- The `[excel]` extra now depends on `drf-spectacular`. `pip install netix-backend[excel]` alone
  failed at import before this: `excel.py` imports `drf_spectacular.utils` at module scope while
  drf-spectacular sat only in `[spectacular]`.
- `netix_backend.django.testing` grew a module-level `__getattr__`; the pytest-plugin module scope
  is still free of django and envoy imports (the two middlewares live in
  `netix_backend.django.testing_middleware` and load on first attribute access).

### Migration notes

- **Behaviour-preserving by default.** `redis_caches(ignore_exceptions=False)` keeps today's
  fleet-wide behaviour (a Redis/Sentinel failover still surfaces as a 500 on 11 services); flipping
  it is a separate, announced change. The multi-host `SENTINEL_HOSTS` parser is opt-in via
  `sentinel_hosts=`, not the unconditional implementation — turning it on is a semantic change for
  the other 12 (a new env var takes precedence and an empty host list becomes `ImproperlyConfigured`
  at import instead of a runtime `MasterNotFoundError`).
- `DEFAULT_TEST_PLATFORM_IDENTITY` is an **opt-in** constant, not an implicit base.
  `DEFAULT_ENVOY_IDENTITY` (three keys) remains the default the two middlewares fall back to, which
  is exactly cafm-backend's current identity. Adopting the superset in cafm-backend or
  visualization-backend adds `is_platform_internal` / `user_type` semantics their suites do not have
  today.
- `EnvoyTestAuthorizationMiddleware.header_guard` defaults **True** (asset/tag/data-service
  behaviour): a request that brings its own `Authorization` header is resolved for real. update-service
  and stormbreaker_backend used `META.setdefault` with no guard, so some of their permission-denial
  tests pass vacuously; adopting the library fixes that, and it should land as a visible change in
  those two repos rather than inside the deduplication PR.
- `db_timeout.install()` must be called explicitly (settings preamble or `AppConfig.ready()`). The
  ten repo copies register via an `@receiver` import side effect, so the `install()` line is
  mandatory at adoption or the ceiling silently stops being applied.
- Adopting the excel core: pin `pagination_class` per repo with `limit_offset_pagination(...)` —
  the four local export paginators are 200/2000, 20000/100000, env-driven, and 1000/1000, none of
  which is the library's 2000/10000 default. tag-service should adopt `ExcelExportViewSet` (no
  `schema`), otherwise adoption adds xlsx schema output to its published OpenAPI contract.
- `org_bootstrap`: each service's `CloneError` must subclass (or alias)
  `netix_backend.django.org_bootstrap.CloneError`, or be passed as `error=` — the six existing
  definitions are unrelated one-line `RuntimeError` subclasses. DELETE now logs the refusal too;
  the six copies swallowed the teardown message.

## Proposed README sections

### Layout table rows

| Module | Contents |
|---|---|
| `netix_backend.cloning` | `TEMPLATE_ORG_ID`, `ORG_KEY_PREFIX`, `PROVENANCE_FIELDS`, `org_prefix`, `base_key`, `org_key`, `key_owner`, `is_org_key` — importable without Django configured |
| `netix_backend.django.db_timeout` | `install()` / `statement_timeout_ms()` — per-connection Postgres `statement_timeout` |
| `netix_backend.django.cache` | `redis_caches()`, `cache_ttls()` — the Redis/Sentinel `CACHES` block and the TTL names |
| `netix_backend.django.org_bootstrap` | `build_org_bootstrap_view()`, `CloneError`, the driver protocols |
| `netix_backend.django.excel` | envoy-free export core: style presets, `ExcelExportViewSet`, `DocumentedExcelViewSet` (needs the `excel` extra) |
| `netix_backend.django.excel_envoy` | `ScopedExcelViewSet` (= `BaseExcelViewSet`), scoping on top of the core |
| `netix_backend.django.excel_aio` | `AsyncExcelViewSet` (needs the `excel` and `async` extras) |
| `netix_backend.django.testing` | fixtures, `netix_test_settings()`, `swap_auth_middleware()`, `assert_timeout_invariant()`, the two test middlewares |

### "Statement timeout" section

```python
# app/settings.py — first thing, before DATABASES
from netix_backend.django import db_timeout

DB_STATEMENT_TIMEOUT_MS = db_timeout.statement_timeout_ms()
db_timeout.install()  # or from your AppConfig.ready()
```

`statement_timeout_ms()` reads `DB_STATEMENT_TIMEOUT_MS` (default 30000) at call time, and returns
`0` while `sys.argv[1]` is a migration command. Every new Postgres connection is `SET` explicitly —
including to 0 — because PgBouncer leaks a prior client's ceiling onto a pooled connection. Keep
`REQUEST_TIMEOUT_SECONDS > DB_STATEMENT_TIMEOUT_MS / 1000`; assert it with
`netix_backend.django.testing.assert_timeout_invariant()`.

### "Cache settings" section

```python
# app/settings.py
from netix_backend.django.cache import cache_ttls, redis_caches

_redis = redis_caches(ignore_exceptions=env_bool("REDIS_IGNORE_EXCEPTIONS"))
CACHES = _redis.caches
SENTINELS = _redis.sentinels  # tag-service reuses this for its channel layer
DJANGO_REDIS_CONNECTION_FACTORY = _redis.connection_factory
DJANGO_REDIS_IGNORE_EXCEPTIONS = _redis.ignore_exceptions
DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = _redis.log_ignored_exceptions
DJANGO_REDIS_LOGGER = _redis.logger_name  # ignore_logger(DJANGO_REDIS_LOGGER) still works

_ttls = cache_ttls(long_ttl_factor=24)
CACHE_TTL, CACHE_LONG_TTL, CACHE_ENABLED = _ttls.ttl, _ttls.long_ttl, _ttls.enabled
SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"
```

Bind by name — never `globals().update(...)`: prospector and mypy are the CI gate, and both
`SENTINELS` and `DJANGO_REDIS_LOGGER` are referenced later in tag-service's and asset-service's own
settings modules. `strict=True` (the default) mirrors `os.environ[...]`; `strict=False` mirrors
visualization-backend's `env()`-with-defaults style.

### "Test identity" section

```python
# app/settings_test.py
from netix_backend.django.testing import platform_test_identity, swap_auth_middleware

from . import settings as base_settings

NETIX_TEST_ENVOY_IDENTITY = platform_test_identity(permissions=["asset-map-write"])
NETIX_TEST_ENVOY_BEARER = "Bearer asset-service-test-platform"
MIDDLEWARE = swap_auth_middleware(base_settings.MIDDLEWARE)
```

`swap_auth_middleware()` defaults to `EnvoyTestAuthorizationMiddleware`, which injects the bearer and
resolves it at envoy-pyauth v2's `_resolve` seam; pass `EXPLICIT_MIDDLEWARE_PATH` for the variant that
assigns `request.envoy` directly. Both fall back to `DEFAULT_ENVOY_IDENTITY` when
`NETIX_TEST_ENVOY_IDENTITY` is unset. A request that already carries an `Authorization` header is
resolved for real, so permission-denial tests stay honest; subclass with `header_guard = False` to
keep the old unguarded behaviour.

### "Cloned primitives" section

```python
from netix_backend.cloning import base_key, key_owner, org_key

org_key("fire_alarm", 5)  # "nc5_fire_alarm"
org_key("nc3_fire_alarm", 5)  # "nc5_fire_alarm" — re-homing replaces, never nests
base_key("nc3_fire_alarm")  # "fire_alarm"
key_owner("fire_alarm")  # None
```

Display columns are never prefixed: a clone keeps its template's `display_name` byte-identical,
because that is the cross-service name contract.
