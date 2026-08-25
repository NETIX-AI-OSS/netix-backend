# netix-backend

Shared Django/DRF base layer for NETIX backend services: soft-delete models, tenant-scoped
viewsets, the `{"status_code": ..., "messages": ...}` error envelope, limit/offset pagination,
HTTP retry transports, a generated-client factory, static-service uploads, and env parsing.

One package, one version. Import only what you need — an unused subpackage costs nothing.

## Install

```toml
# pyproject.toml
dependencies = [
    "netix-backend @ git+https://github.com/NETIX-AI-OSS/netix-backend.git@v1.1.0",
]
```

Extras: `[spectacular]` (OpenAPI schema helpers), `[async]` (adrf viewsets), `[excel]` (xlsx export),
`[otel]` (OpenTelemetry bootstrap).

## Layout

| Module | Contents |
|---|---|
| `netix_backend.env` | `env_bool`, `env_bool_strict`, `env_str`, `env_int`, `env_float`, `env_first`, `service_authorization` — importable without Django configured |
| `netix_backend.http` | `RetryTransport` / `AsyncRetryTransport` / `RetryTransportWrapper`, `build_client`, `request_with_retry` / `post_with_retry`, `static_upload` / `static_fetch`, healthz helpers — importable without Django configured |
| `netix_backend.asgi` | `RequestTimeoutMiddleware`, `LifespanWrapper` / `wrap`, `ServiceRegistrationASGI` / `ServiceRegistrationWSGI` / `trigger_service_registration`, `cors_headers`, `error_messages` — importable without Django configured |
| `netix_backend.asgi.testing` | `RequestTimeoutContract` (subclass it), `slow_app`, `drive`, `ServerCycle`, `slow_view`, the opt-in assertions |
| `netix_backend.observability.otel` | `configure()`, `is_configured()`, `reset_for_tests()` — OTel bootstrap, importable without the SDK (needs the `otel` extra to actually trace) |
| `netix_backend.observability.logging` | `logging_config()` dictConfig factory, `CONSOLE_FORMAT`, `TRACE_ID_FIELDS`, `TRACE_ID_DEFAULTS`, `ContextFormatter`, `log_context` |
| `netix_backend.observability.sentry_filters` | `chain`, `drop_cancelled_errors`, `drop_client_errors`, `group_log_events_by_template`, `drop_matching_signatures`, `fingerprint_matching_signatures`, `IGNORED_TELEMETRY_LOGGERS` |
| `netix_backend.cloning` | `TEMPLATE_ORG_ID`, `ORG_KEY_PREFIX`, `PROVENANCE_FIELDS`, `org_prefix`, `base_key`, `org_key`, `key_owner`, `is_org_key` — importable without Django configured |
| `netix_backend.django.models` | `BaseModel`, `NamedBaseModel`, `SluggedNamedBaseModel`, `CompactNamedBaseModel`, `BaseManager`, `CloneProvenanceMixin`, `organization_scoped()` |
| `netix_backend.django.views` | `BaseViewSet` and its mixins (scoping, tenant write pinning, atomic writes, soft delete, permissions); `include_deleted_schema()` needs the `spectacular` extra |
| `netix_backend.django.views_aio` | `AsyncBaseViewSet` (requires the `async` extra) |
| `netix_backend.django.exceptions` | `custom_exception_handler` + message flatteners, behind `NETIX_ERRORS_*` settings |
| `netix_backend.django.pagination` | `limit_offset_pagination()` factory, `BaseLimitOffsetPagination`, `BigLimitOffsetPagination` |
| `netix_backend.django.serializers` | `BASE_FIELDS`, `NAMED_BASE_FIELDS`, `NamedBaseSerializer` |
| `netix_backend.django.filters` | `CharArrayFilter`, `CharInFilter`, `NumberInFilter`, `IntArrayFilter`, `NumberArrayFilter`, `BASE_FILTERS` (requires the `spectacular` extra) |
| `netix_backend.django.schema` | `AsyncActionAutoSchema`, `XlsxExportAutoSchema`, `XLSX_BINARY_RESPONSE` (requires the `spectacular` extra) |
| `netix_backend.django.excel` | envoy-free export core: style presets, `ExcelExportViewSet`, `DocumentedExcelViewSet` (requires the `excel` extra) |
| `netix_backend.django.excel_envoy` | `ScopedExcelViewSet` (= `BaseExcelViewSet`), scoping on top of the excel core |
| `netix_backend.django.excel_aio` | `AsyncExcelViewSet` (requires the `excel` and `async` extras) |
| `netix_backend.django.db_timeout` | `install()` / `statement_timeout_ms()` — per-connection Postgres `statement_timeout` |
| `netix_backend.django.cache` | `redis_caches()`, `cache_ttls()` — the Redis/Sentinel `CACHES` block and the TTL names |
| `netix_backend.django.org_bootstrap` | `build_org_bootstrap_view()`, `CloneError`, the `ClonePrimitives` / `TeardownPrimitives` protocols |
| `netix_backend.django.testing` | `unscoped_envoy` / `explicit_envoy_identity` pytest fixtures, `netix_test_settings()`, `swap_auth_middleware()`, `platform_test_identity()`, `assert_timeout_invariant()`, the two test middlewares |

`BaseViewSet` scopes reads through `model_queryset` (or an explicit `_get_queryset_filter()` call in a
`get_queryset` override). A plain DRF `queryset = …` attribute is handed back unscoped, exactly as in the
services today — declare `model_queryset` to get organization scoping.

Behavioral knobs default to what the fleet ships today; behavior changes are opt-in per service.
See `CHANGELOG.md` for migration notes per release.

## ASGI entrypoints

```python
# app/asgi.py
from netix_backend.asgi import LifespanWrapper, RequestTimeoutMiddleware, ServiceRegistrationASGI

application = get_asgi_application()
if settings.DEBUG or os.environ.get("DJANGO_SERVE_STATIC", "TRUE") == "TRUE":
    application = ASGIStaticFilesHandler(application)

# Lifespan outermost: ASGIStaticFilesHandler hands every non-http scope straight to ASGIHandler.
# Ceiling outside the Django handler: an entry in MIDDLEWARE stalls the event loop instead of protecting it.
application = LifespanWrapper(ServiceRegistrationASGI(RequestTimeoutMiddleware(application)))
```

The ceiling reads `REQUEST_TIMEOUT_SECONDS` (default 120) and `MAX_ABANDONED_REQUESTS` (default 50)
at import, and resolves them per instance in `__init__`, so both stay patchable in tests. Only
`GET` / `HEAD` / `OPTIONS` are capped by default — a 504 is retryable, so answering one mid-write
turns one write into two. Pass `safe_methods=None` to cap every method, `frozenset()` to cap none.
Registration is env-driven (`SERVICE_REGISTRATION_ENABLED`, `SERVICE_NAME`, …,
`UPDATE_SERVICE_REGISTRATION_URL`, `SERVICE_REGISTRATION_TOKEN`) and starts once per process.

Guard the ceiling with the shared contract suite:

```python
# service/tests/test_request_timeout.py
from django.urls import path
from netix_backend.asgi.testing import RequestTimeoutContract, slow_view

urlpatterns = [path("slow/", slow_view)]


class TestRequestTimeout(RequestTimeoutContract):
    urlconf = __name__  # opt in to the event-loop check
    forbid_middleware_entry = True  # opt in to "no timeout entry in settings.MIDDLEWARE"
    db_statement_timeout_ms = db_timeout.DB_STATEMENT_TIMEOUT_MS  # only where db_timeout is adopted
```

## Observability

Extras: `[otel]` (the seven `opentelemetry-*` packages). `netix_backend.observability` imports
cleanly without Django configured and without OpenTelemetry or `sentry_sdk` installed — every
`opentelemetry` import is function-local and `sentry_sdk` is never imported at all, so the
filters are plain functions over the event/hint dicts.

```python
# manage.py and app/asgi.py, before django.setup()
from netix_backend.observability.otel import configure

configure("my-service")
```

```python
# app/settings.py
from netix_backend.observability.logging import ContextFormatter, logging_config
from netix_backend.observability.sentry_filters import (
    chain,
    drop_cancelled_errors,
    drop_matching_signatures,
    group_log_events_by_template,
)

LOGGING = logging_config(
    root_level=os.getenv("DJANGO_LOG_LEVEL", "WARN"),
    loggers={"django": os.getenv("DJANGO_LOG_LEVEL", "INFO"), "django.template": "INFO"},
    formatter_class=ContextFormatter,  # opt-in; omit for a plain logging.Formatter
)

_TRANSIENT_INFRA_SIGNATURES = ("No master found for",)  # keep service-specific lists local
before_send = chain(
    drop_cancelled_errors,
    drop_matching_signatures(_TRANSIENT_INFRA_SIGNATURES),
    group_log_events_by_template,
)
```

`configure()` is idempotent and env-driven: it no-ops unless a collector endpoint is
configured, honours `OTEL_SDK_DISABLED`, and lets `OTEL_SERVICE_NAME` from the ConfigMap win
over the code argument. Call it from both `manage.py` and `app/asgi.py`, post-fork — the
`BatchSpanProcessor` export thread must be created in the worker. Tests reset the process
latch with `reset_for_tests()`, never `importlib.reload`.

`logging_config()` keeps the `defaults={"otelTraceID": "0", "otelSpanID": "0"}` that stops the
formatter raising on every record when tracing is off; without it Loki loses the line entirely
and cannot join to Tempo.

## Statement timeout

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

## Cache settings

```python
# app/settings.py
from netix_backend.django.cache import cache_ttls, redis_caches

_redis = redis_caches(ignore_exceptions=env_bool("REDIS_IGNORE_EXCEPTIONS"))
CACHES = _redis.caches
SENTINELS = _redis.sentinels
DJANGO_REDIS_CONNECTION_FACTORY = _redis.connection_factory
DJANGO_REDIS_IGNORE_EXCEPTIONS = _redis.ignore_exceptions
DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = _redis.log_ignored_exceptions
DJANGO_REDIS_LOGGER = _redis.logger_name

_ttls = cache_ttls(long_ttl_factor=24)
CACHE_TTL, CACHE_LONG_TTL, CACHE_ENABLED = _ttls.ttl, _ttls.long_ttl, _ttls.enabled
```

Bind by name — never `globals().update(...)`. `strict=True` (the default) mirrors
`os.environ[...]`; `strict=False` mirrors the `env()`-with-defaults style.

## Test identity

```python
# app/settings_test.py
from netix_backend.django.testing import platform_test_identity, swap_auth_middleware

from . import settings as base_settings

NETIX_TEST_ENVOY_IDENTITY = platform_test_identity(permissions=["asset-map-write"])
NETIX_TEST_ENVOY_BEARER = "Bearer asset-service-test-platform"
MIDDLEWARE = swap_auth_middleware(base_settings.MIDDLEWARE)
```

`swap_auth_middleware()` defaults to `EnvoyTestAuthorizationMiddleware`, which injects the bearer and
resolves it at envoy-pyauth's `_resolve` seam; pass `EXPLICIT_MIDDLEWARE_PATH` for the variant that
assigns `request.envoy` directly. A request that already carries an `Authorization` header is
resolved for real, so permission-denial tests stay honest; subclass with `header_guard = False` to
keep the old unguarded behaviour.

## Cloned primitives

```python
from netix_backend.cloning import base_key, key_owner, org_key

org_key("fire_alarm", 5)  # "nc5_fire_alarm"
org_key("nc3_fire_alarm", 5)  # "nc5_fire_alarm" — re-homing replaces, never nests
base_key("nc3_fire_alarm")  # "fire_alarm"
key_owner("fire_alarm")  # None
```

Display columns are never prefixed: a clone keeps its template's `display_name` byte-identical,
because that is the cross-service name contract.

## Development

```bash
uv sync --all-groups --all-extras
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy netix_backend
uv run prospector --without-tool pylint --max-line-length 120
```

Releases are annotated 3-component tags (`vX.Y.Z`) matching `[project].version`; tags are
immutable via repo ruleset. Consumers are kept current by `netix deps check` in the
NETIX-AI/monorepo workspace tooling.
