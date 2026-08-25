# v1.1.0 notes — `netix_backend.observability`

Proposed CHANGELOG bullets and README section for the observability subpackage. Merge these into
`CHANGELOG.md` / `README.md` when the v1.1.0 branches are integrated.

## Proposed CHANGELOG bullets

```markdown
### Added

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
