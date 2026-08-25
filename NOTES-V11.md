# v1.1.0 notes — `netix_backend.asgi` (merge into CHANGELOG.md / README.md, then delete)

# v1.1.0 notes — `netix_backend.observability`

Proposed CHANGELOG bullets and README section for the observability subpackage. Merge these into
`CHANGELOG.md` / `README.md` when the v1.1.0 branches are integrated.

## Proposed CHANGELOG bullets

```markdown
### Added

- `netix_backend.asgi` — the ASGI/WSGI entrypoint layer, importable without Django configured:
  - `asgi.timeout.RequestTimeoutMiddleware`, the wall-clock request ceiling that answers 504,
    abandons (never cancels) the in-flight task, mutes the wrapped app's `send` once it has
    replied, and sheds with 503 + `Retry-After` past `MAX_ABANDONED_REQUESTS`. Ships the gen-3
    behaviour of 13 services verbatim, including the bare-string `{"status_code", "messages"}`
    body and the `REQUEST_TIMEOUT_SECONDS` / `MAX_ABANDONED_REQUESTS` env names the
    user-management ConfigMap and its auth deployment already override. New knobs:
    `safe_methods=None` caps every method (an empty set caps none), `timeout_setting=` resolves
    the ceiling from a Django setting lazily in `__init__`, and `error_body=` swaps the reply
    renderer (`error_messages` envelope or `text_messages` text/plain).
  - `asgi.lifespan.wrap()` / `LifespanWrapper` / `serve_lifespan()` — the lifespan handshake
    Django does not implement, extracted from gateway-service's local version. Pure: it no
    longer triggers service registration on the way past.
  - `asgi.registration` — the env-driven update-service self-registration client, its fixed
    `RETRY_DELAYS = (0, 1, 2, 4, 8, 16, 30, 60)` ladder on a daemon thread, the once-per-process
    latch with a `reset_registration_state()` test hook, and the `ServiceRegistrationASGI` /
    `ServiceRegistrationWSGI` wrappers. Stdlib urllib only — no httpx, no Django — so it stays
    importable in a pre-`django.setup()` boot path. Re-exported as `netix_backend.registration`.
  - `asgi.types` — shared `Scope` / `Receive` / `Send` / `Message` / `ASGIApp` / `WSGIApp` aliases.
  - `asgi.testing.RequestTimeoutContract` — the ~307-line contract suite 15 repos copy-paste,
    as a subclassable pytest class plus its harness (`slow_app`, `drive`, `ServerCycle`,
    `slow_view`, `django_scope`). The `db_timeout` cross-assertion, the "no `timeout` entry in
    `settings.MIDDLEWARE`" assertion and the event-loop check are opt-in knobs, since only some
    repos can make them.
```

## Proposed README section

Add to the layout table (after the `netix_backend.http` row):

| Module | Contents |
|---|---|
| `netix_backend.asgi` | `RequestTimeoutMiddleware`, `LifespanWrapper` / `wrap`, `ServiceRegistrationASGI` / `ServiceRegistrationWSGI` / `trigger_service_registration`, `cors_headers`, `error_messages` — importable without Django configured |
| `netix_backend.asgi.testing` | `RequestTimeoutContract` (subclass it), `slow_app`, `drive`, `ServerCycle`, `slow_view`, the two opt-in assertions |

And a short prose section:

### ASGI entrypoints

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

Guard it with the shared contract suite:

```python
# service/tests/test_request_timeout.py
from django.urls import path
from netix_backend.asgi.testing import RequestTimeoutContract, slow_view

urlpatterns = [path("slow/", slow_view)]


class TestRequestTimeout(RequestTimeoutContract):
    urlconf = __name__  # opt in to the event-loop check
    forbid_middleware_entry = True  # opt in to "no timeout entry in settings.MIDDLEWARE"
    db_statement_timeout_ms = db_timeout.DB_STATEMENT_TIMEOUT_MS  # only where app/db_timeout.py exists
```

## Adoption notes per repo (for the migration PRs, not for the README)

- **13 gen-3 repos** (asset-service, backend-template, data-service, gateway-service,
  license-server, ml-engine, notification-service, redirect-service, stormbreaker_backend,
  tag-service, update-service, user-management, visualization-backend): delete
  `app/timeout_middleware.py`, import `RequestTimeoutMiddleware` from `netix_backend.asgi`.
  Wire behaviour is byte-identical; user-management's ConfigMap/deployment env overrides keep
  working because the env names are unchanged.
- **All 18 repos**: delete `app/service_registration.py` and import from `netix_backend.asgi`.
  Their 18 `test_service_registration.py` files patch `app.service_registration` internals
  (`_REGISTRATION_STARTED` in 11, `REGISTRATION_STATE` in 6) — repoint them at
  `netix_backend.asgi.registration` and use `reset_registration_state()` instead of either name.
- **5 inline-registration repos** (backend-template, asset-service, data-service, license-server,
  cafm-backend): their `LifespanWrapper` calls `trigger_service_registration()` in the non-lifespan
  branch. The library wrapper is pure, so wrap with the already-present `ServiceRegistrationASGI`
  when adopting: `LifespanWrapper(ServiceRegistrationASGI(RequestTimeoutMiddleware(app)))`.
- **5 registration-outermost repos** (static-service, user-management, redirect-service,
  stormbreaker_backend, tag-service): adopting the documented stacking flips their order to
  lifespan-outermost. Safe (registration passes non-http scopes through) but it is a real change.
- **tag-service**: its inline `application()` fuses lifespan with a `/healthz/` short-circuit —
  migrate to `wrap()` + `netix_backend.http.healthz_asgi`.
- **cafm-backend / simulator-service** (gen-2): pass `timeout_setting="REQUEST_TIMEOUT"` to keep
  cafm's lazily-resolved ceiling, and set `REQUEST_TIMEOUT_SECONDS=30` in the ConfigMap to keep
  the old default. They gain CORS reproduction, the JSON envelope and 503 shedding; pass
  `error_body=text_messages` to keep today's text/plain body, and a large `max_abandoned` to keep
  shedding off during the first rollout.
- **static-service**: convertible, but it is a real behaviour change — its middleware is
  `MIDDLEWARE`-mounted and uses `asyncio.wait_for`, i.e. it cancels. Move the ceiling into
  `app/asgi.py` and drop the `MIDDLEWARE` entry, then set `forbid_middleware_entry = True`.
- **report-engine**: out of scope. Its middleware is `gevent.Timeout` with
  `sync_capable=True / async_capable=False`, and gevent is the deploy model — there is no ASGI
  wrapper path without changing its server. It can still adopt `asgi.registration`.

## Follow-ups deliberately not done here

- `netix_backend/http/health.py` still declares its own `Scope` / `Receive` / `Send` aliases; they
  should re-import from `netix_backend.asgi.types` in a later pass (that file is outside this
  change's scope).
- No lifespan contract suite is shipped; gateway-service's `app/tests/test_lifespan.py` is the only
  copy and has no fleet duplication to collapse.

- `netix_backend.observability.otel` — the OpenTelemetry bootstrap vendored in 17 services
  (`configure(service_name=None)`, `is_configured()`, `reset_for_tests()`). Every
  `opentelemetry` import stays function-local, so `manage.py` can call `configure()` before
  `django.setup()` and a service without the new `[otel]` extra still imports and boots. Ships
  the `opentelemetry-instrumentation-asgi` guard that stops the Django instrumentor silently
  skipping every uvicorn request, and honours `OTEL_TRACES_SAMPLER` through a guarded fallback
  so a 1.x SDK bump that drops the private `_get_from_env_or_default` degrades to the SDK
  default instead of failing boot. New `[otel]` extra carries the seven `opentelemetry-*` pins.
- `netix_backend.observability.logging` — `CONSOLE_FORMAT`, `TRACE_ID_FIELDS`,
  `TRACE_ID_DEFAULTS`, `DEFAULT_LOGGERS`, `logging_config()`, plus `ContextFormatter` /
  `log_context()` / `LOG_CONTEXT_KEY` lifted from the three `app/log_formatters.py` copies.
  `otel.configure()` imports `TRACE_ID_FIELDS` from the same module, so the format string and
  the `LoggingInstrumentor` injection stop agreeing only by copy-paste.
- `netix_backend.observability.sentry_filters` — composable `before_send` filters as pure
  functions over event dicts, with no `sentry_sdk` import anywhere in the library:
  `drop_cancelled_errors`, `drop_client_errors(*classes, status_range=…)`,
  `group_log_events_by_template`, `drop_matching_signatures`,
  `fingerprint_matching_signatures`, the `hint_exception` / `exception_chain_text` /
  `event_text_candidates` / `event_text` extraction helpers, `IGNORED_TELEMETRY_LOGGERS`, and
  `chain(*filters)`.

### Fixed (on adoption)

- `drop_cancelled_errors` scans the whole `exception.values` list and the hint's
  `originalException`, not just `values[-1]`. redirect-service and visualization-backend drop a
  `CancelledError` only when it is the terminal frame, and never when it arrives through the
  logging integration; adopting closes both gaps.
- `chain()` wraps each filter so a raising filter is logged and skipped rather than taking the
  whole `before_send` down — a `before_send` that raises drops **every** event.
- `event_text_candidates` carries the two `isinstance` guards cafm-backend lacks
  (`isinstance(hint, Mapping)`, `isinstance(exc_data, Mapping)`) and the cycle-guard
  data-service's cut-down `_asgi_event_text` lacks, so a self-referential `__context__` chain
  no longer loops forever.

### Migration notes

- Adopting `group_log_events_by_template` in visualization-backend is a **deliberate
  fingerprint change**, not a drop-in: that repo groups on `[logger, level, template]` from
  `logentry`, the library groups on `[record.name, record.msg, exc_type]` from
  `hint["log_record"]`. Its existing GlitchTip issues re-group on deploy.
- `logging_config(formatter_class=…)` defaults to **plain `logging.Formatter`**. The three
  `ContextFormatter` repos must pass it explicitly; flipping the default for the other fourteen
  is a separate v1.2 decision.
- `reset_for_tests()` replaces `importlib.reload(app.otel)` in the 17 local `test_otel.py`
  suites. Reloading a shared library module detaches every previously-imported reference — do
  not port the reload fixture.
- Adopting the `[otel]` extra means dropping the seven direct `opentelemetry-*` pins from the
  service's own `pyproject.toml`; `netix deps check` sees a double pin otherwise.
- Adoption must not touch `sentry_sdk.init(environment=…)`. See the note below.

## Proposed README section

Add to the Layout table:

| Module | Contents |
|---|---|
| `netix_backend.observability.otel` | `configure()`, `is_configured()`, `reset_for_tests()` — OTel bootstrap, importable without the SDK (needs the `otel` extra to actually trace) |
| `netix_backend.observability.logging` | `logging_config()` dictConfig factory, `CONSOLE_FORMAT`, `TRACE_ID_FIELDS`, `TRACE_ID_DEFAULTS`, `ContextFormatter`, `log_context` |
| `netix_backend.observability.sentry_filters` | `chain`, `drop_cancelled_errors`, `drop_client_errors`, `group_log_events_by_template`, `drop_matching_signatures`, `fingerprint_matching_signatures`, `IGNORED_TELEMETRY_LOGGERS` |

And a prose block:

> ### Observability
>
> Extras: `[otel]` (the seven `opentelemetry-*` packages). `netix_backend.observability` imports
> cleanly without Django configured and without OpenTelemetry or `sentry_sdk` installed — every
> `opentelemetry` import is function-local and `sentry_sdk` is never imported at all, so the
> filters are plain functions over the event/hint dicts.
>
> ```python
> # manage.py and app/asgi.py, before django.setup()
> from netix_backend.observability.otel import configure
> configure("my-service")
> ```
>
> ```python
> # app/settings.py
> from netix_backend.observability.logging import ContextFormatter, logging_config
> from netix_backend.observability.sentry_filters import (
>     IGNORED_TELEMETRY_LOGGERS, chain, drop_cancelled_errors,
>     drop_matching_signatures, group_log_events_by_template,
> )
>
> LOGGING = logging_config(
>     root_level=os.getenv("DJANGO_LOG_LEVEL", "WARN"),
>     loggers={"django": os.getenv("DJANGO_LOG_LEVEL", "INFO"), "django.template": "INFO"},
>     formatter_class=ContextFormatter,   # opt-in; omit for a plain logging.Formatter
> )
>
> _TRANSIENT_INFRA_SIGNATURES = ("No master found for", ...)   # keep service-specific lists local
> before_send = chain(
>     drop_cancelled_errors,
>     drop_matching_signatures(_TRANSIENT_INFRA_SIGNATURES),
>     group_log_events_by_template,
> )
> ```
>
> `configure()` is idempotent and env-driven: it no-ops unless a collector endpoint is
> configured, honours `OTEL_SDK_DISABLED`, and lets `OTEL_SERVICE_NAME` from the ConfigMap win
> over the code argument. Call it from both `manage.py` and `app/asgi.py`, post-fork — the
> `BatchSpanProcessor` export thread must be created in the worker. Tests reset the process
> latch with `reset_for_tests()`, never `importlib.reload`.
>
> `logging_config()` keeps the `defaults={"otelTraceID": "0", "otelSpanID": "0"}` that stops the
> formatter raising on every record when tracing is off; without it Loki loses the line entirely
> and cannot join to Tempo.

## SENTRY_ENVIRONMENT — do not "tidy" this during adoption

Surveyed while extracting the filters; **out of scope for this change and must not be folded
into an adoption PR**, because `environment` is what GlitchTip tags every issue with and what
saved searches and alert rules filter on.

- 12 repos hardcode `environment="main"`: backend-template, cafm-backend, gateway-service,
  license-server, notification-service, redirect-service, report-engine, simulator-service,
  static-service, stormbreaker_backend, update-service, visualization-backend.
- 4 read `os.environ.get("SENTRY_ENVIRONMENT", "main")`: asset-service, data-service, ml-engine,
  and tag-service (via a module-level `SENTRY_ENVIRONMENT` binding).
- 1 reads `os.environ.get("SENTRY_ENVIRONMENT", "unknown")`: user-management — the only repo
  whose fallback is not `"main"`.
- **Live mismatch:** `stormbreaker_backend/stormbreaker-configmap.yaml` sets
  `SENTRY_ENVIRONMENT: 'gcp-dev'` (both blocks) while `app/settings.py:253` hardcodes
  `environment="main"`, so the ConfigMap key is silently ignored there. Fixing it re-tags every
  stormbreaker issue and belongs in its own PR with the monitoring owners.

Two other gaps found in the same survey, also separate PRs:

- backend-template — the canonical scaffold — has neither a `before_send` nor the ignored-logger
  tuple; it inits with `LoggingIntegration(event_level=None)`, a fourth strategy. Until the
  template adopts, every new service starts unfiltered.
- report-engine calls `sentry_sdk.init` with no `before_send` at all.
- vision-ai-service has neither `app/otel.py` nor any `opentelemetry` pin, and its `LOGGING`
  (`app/settings.py:213`) is a bare `StreamHandler` with no `formatters` key, so it emits no
  trace/span ids and cannot be joined Loki→Tempo. That is a **new capability** (deps + ConfigMap
  `OTEL_*` keys), not a deletion — treat it as its own enablement PR.
