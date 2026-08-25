# Changelog

## v1.1.0 — 2026-08-25

Second extraction wave: an opus research sweep across the 18 services surfaced twelve more
cross-repo duplications; all twelve ship here. Defaults preserve today's fleet behaviour;
deliberate fixes are opt-in per service.

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
    Django does not implement. Pure: it no longer triggers service registration on the way past.
  - `asgi.registration` — the env-driven update-service self-registration client, its fixed
    `RETRY_DELAYS = (0, 1, 2, 4, 8, 16, 30, 60)` ladder on a daemon thread, the once-per-process
    latch with a `reset_registration_state()` test hook, and the `ServiceRegistrationASGI` /
    `ServiceRegistrationWSGI` wrappers. Stdlib urllib only, so it stays importable in a
    pre-`django.setup()` boot path. Re-exported as `netix_backend.registration`.
  - `asgi.types` — shared `Scope` / `Receive` / `Send` / `Message` / `ASGIApp` / `WSGIApp` aliases.
  - `asgi.testing.RequestTimeoutContract` — the contract suite 15 repos copy-paste, as a
    subclassable pytest class plus its harness (`slow_app`, `drive`, `ServerCycle`, `slow_view`,
    `django_scope`). The `db_timeout` cross-assertion, the "no `timeout` entry in
    `settings.MIDDLEWARE`" assertion and the event-loop check are opt-in knobs.
- `netix_backend.observability.otel` — the OpenTelemetry bootstrap vendored in 17 services
  (`configure(service_name=None)`, `is_configured()`, `reset_for_tests()`). Every
  `opentelemetry` import stays function-local, so a service without the new `[otel]` extra
  still imports and boots. Ships the `opentelemetry-instrumentation-asgi` guard that stops the
  Django instrumentor silently skipping every uvicorn request, and honours `OTEL_TRACES_SAMPLER`
  through a guarded fallback so an SDK bump that drops the private `_get_from_env_or_default`
  degrades to the SDK default instead of failing boot. New `[otel]` extra carries the seven
  `opentelemetry-*` pins.
- `netix_backend.observability.logging` — `CONSOLE_FORMAT`, `TRACE_ID_FIELDS`,
  `TRACE_ID_DEFAULTS`, `DEFAULT_LOGGERS`, `logging_config()`, plus `ContextFormatter` /
  `log_context()` / `LOG_CONTEXT_KEY` lifted from the three `app/log_formatters.py` copies.
  `otel.configure()` imports `TRACE_ID_FIELDS` from the same module, so the format string and
  the `LoggingInstrumentor` injection stop agreeing only by copy-paste.
- `netix_backend.observability.sentry_filters` — composable `before_send` filters as pure
  functions over event dicts, with no `sentry_sdk` import anywhere in the library:
  `drop_cancelled_errors`, `drop_client_errors(*classes, status_range=…)`,
  `group_log_events_by_template`, `drop_matching_signatures`, `fingerprint_matching_signatures`,
  the `hint_exception` / `exception_chain_text` / `event_text_candidates` / `event_text`
  extraction helpers, `IGNORED_TELEMETRY_LOGGERS`, and `chain(*filters)`.
- `netix_backend.cloning` (Django-free) — the org-prefixed machine-key contract seven repos
  carry a private copy of: `TEMPLATE_ORG_ID`, `ORG_KEY_PREFIX`, `PROVENANCE_FIELDS`,
  `org_prefix`, `base_key`, `org_key`, `key_owner`, `is_org_key`. Vendored rather than
  re-exported: envoy-pyauth v2.0.0 ships no `cloning` module and the library's floor is
  `>=2.0.0`. Pairs with the existing `CloneProvenanceMixin`.
- `netix_backend.django.db_timeout` — the per-connection Postgres `statement_timeout` receiver
  (`DB_STATEMENT_TIMEOUT_MS`, `MIGRATION_COMMANDS`, `is_migration_process()`,
  `statement_timeout_ms()`, `install()`, `uninstall()`). PgBouncer rejects the option as a
  startup parameter and leaks a prior client's ceiling onto a pooled connection, so every new
  connection is `SET` explicitly — including `SET statement_timeout = 0` for migration commands.
  Registration is explicit via `install()` rather than an import side effect.
- `netix_backend.django.org_bootstrap` — `build_org_bootstrap_view(clone=…, teardown=…)`,
  `OrgBootstrapBaseView`, the serializers, the `ClonePrimitives` / `TeardownPrimitives`
  protocols and a shared `CloneError`. Each of the six services drops a ~70-line
  `views_bootstrap.py` for a three-line binding.
- `netix_backend.django.cache` — `redis_caches(...)` and `cache_ttls(...)` returning frozen
  `RedisCacheSettings` / `CacheTtls` dataclasses, replacing the `SENTINEL_ENABLED` if/else that
  13 `settings.py` copy verbatim. Emits the whole django_redis knob set so the
  `ignore_logger()` pattern survives adoption.
- `netix_backend.django.testing` — `EnvoyTestAuthorizationMiddleware` and
  `ExplicitEnvoyIdentityMiddleware` (settings-driven via `NETIX_TEST_ENVOY_IDENTITY` /
  `NETIX_TEST_ENVOY_BEARER`), `swap_auth_middleware()`, `platform_test_identity()`, and
  `assert_timeout_invariant()` — the `REQUEST_TIMEOUT_SECONDS > DB_STATEMENT_TIMEOUT_MS / 1000`
  assertion nine repos duplicate.
- `netix_backend.django.excel_aio.AsyncExcelViewSet` — the adrf export shape (`list` via
  `async_to_sync(self.alist)`, plus `alist` / `aretrieve`, all carrying the binary response
  override).

### Changed

- `netix_backend.django.excel` is now an envoy-free core: `ExcelExportMixin`,
  `ExcelExportViewSet` (no `schema`), `DocumentedExcelViewSet`. The scoped variant moved to
  `netix_backend.django.excel_envoy.ScopedExcelViewSet`; `BaseExcelViewSet` is an alias of it
  and stays importable from `netix_backend.django.excel` via a module `__getattr__`, so no
  existing import or subclass breaks. Importing `netix_backend.django.excel` no longer pulls
  `envoy_pyauth`.
- The `[excel]` extra now depends on `drf-spectacular`; `pip install netix-backend[excel]`
  alone failed at import before.

### Fixed (on adoption)

- `drop_cancelled_errors` scans the whole `exception.values` list and the hint's
  `originalException`, not just `values[-1]`.
- `chain()` wraps each filter so a raising filter is logged and skipped rather than dropping
  every event.
- `event_text_candidates` carries the `isinstance` guards and cycle-guard some copies lack.
- `db_timeout` DELETE-path: `org_bootstrap` DELETE now logs the refusal the six copies
  swallowed.

### Migration notes

- Behaviour-preserving by default: `redis_caches(ignore_exceptions=False)` keeps today's
  behaviour; the multi-host `SENTINEL_HOSTS` parser is opt-in via `sentinel_hosts=`.
- `db_timeout.install()` must be called explicitly — the repo copies registered via an
  `@receiver` import side effect; omitting the line silently drops the ceiling.
- `EnvoyTestAuthorizationMiddleware.header_guard` defaults True; update-service and
  stormbreaker_backend gain honest permission-denial tests, which should land as a visible
  change.
- `reset_for_tests()` replaces `importlib.reload(app.otel)` in local otel tests; adopting the
  `[otel]` extra means dropping the seven direct `opentelemetry-*` pins per service.
- Adoption must not touch `sentry_sdk.init(environment=…)` — see the SENTRY_ENVIRONMENT survey
  in the adoption notes; stormbreaker's ConfigMap/env mismatch is its own PR.
- report-engine is out of scope for the request ceiling (gevent deploy model, no ASGI path); it
  can still adopt `asgi.registration`.

## v1.0.2 — 2026-08-23

### Fixed

- v1.0.1's docstring-to-comment conversion missed the multi-line class definitions: `BaseViewSet`
  and `AsyncBaseViewSet` still carried class docstrings that drf-spectacular surfaces as the
  default operation description on every generic CRUD endpoint. Both are comments now, and an
  order-proof regression test asserts no view-MRO class carries a `__doc__` at all. Consumers
  that re-added a `GET_LIB_DOC_EXCLUDES` hook for this can drop it.


## v1.0.1 — 2026-08-23

Fixes from the redirect-service pilot adoption (NETIX-AI/redirect-service#155).

### Changed

- Removed `[tool.uv.sources]` for envoy-pyauth (it leaked into consumer resolution and broke
  `uv lock` for repos pinning their own envoy-pyauth); the dev pin moved into the dev
  dependency-group as a direct URL. Consumers no longer need `override-dependencies`.
- `NETIX_ERRORS_NON_VALIDATION_AS_LIST` now defaults **False**: the template family emits
  `str(exc)` — a bare string — for 401/403/405/500-class errors. The list shape is opt-in
  (simulator-service pins it True).
- `EnvoyPermissionMixin.get_authenticate_header` is now knob-driven (`authenticate_header`,
  default `None`): adopting `BaseViewSet` no longer silently flips failed-auth 403 → 401.
  notification-service pins `authenticate_header = WWW_AUTHENTICATE_HEADER`.
- View-MRO class docstrings converted to comments so drf-spectacular no longer inherits them
  into every operation description (guarded by a schema-generation regression test).
- `post_with_retry` accepts `json=`; `request_with_retry` documents that exhausted status
  retries return the last response rather than raising.

### Migration notes

Consumers that worked around v1.0.0 with `[tool.uv] override-dependencies` or a
`spectacular` docstring-exclusion hook can drop both on upgrade.

## v1.0.0 — 2026-08-23

Initial release: the shared base layer extracted from the 18 NETIX backend services.

### Added

- `netix_backend.env` — env parsing (`env_bool`, `env_bool_strict`, `env_str`, `env_int`,
  `env_float`, `env_first`, `service_authorization`, `ConfigurationError`).
- `netix_backend.http` — `RetryTransport`/`AsyncRetryTransport` (idempotent-safe status retries,
  equal-jitter backoff, Retry-After incl. HTTP-date, `retries=0` disables), `build_client`
  generated-client factory, `request_with_retry`/`post_with_retry` (deadline-capped),
  `static_upload`/`static_fetch` (accepts 200-on-dedup and 201, required `retention_date`),
  `healthz_view`/`healthz_asgi`.
- `netix_backend.django.models` — `BaseModel` (auto_now + `update_fields` injection,
  IntegrityError→ValidationError in `save` and `asave`, `soft_delete`/`asoft_delete`,
  `validate_soft_delete` hook), `NamedBaseModel`, `SluggedNamedBaseModel`,
  `CompactNamedBaseModel`, `BaseManager` (`get_or_none`/`aget_or_none`), `BaseQuerySet`
  (`alive`/`dead`/`stamp_and_update`), `CloneProvenanceMixin`, `organization_scoped()`.
- `netix_backend.django.views` — `BaseViewSet` composed from `EnvoyPermissionMixin`,
  `EnvoyScopedQuerysetMixin` (keyword-only superset signature, works against envoy-pyauth v2 and
  v3), `TenantWriteScopeMixin` (`off`/`pin`/`pin_and_reject`), `AtomicWriteMixin`,
  `SoftDeleteMixin`, `ListErrorMappingMixin`, `IncludeDeletedMixin`,
  `CreateListModelMixin`/`UpdateListModelMixin`.
- `netix_backend.django.views_aio` — `AsyncBaseViewSet` honoring sync `perform_create`/`perform_update` overrides.
- `netix_backend.django.exceptions` — shared handler with `NETIX_ERRORS_STRINGIFIED` (default
  True: today's wire shape), pluggable flatteners (`template`, `keyed`, `first_error`, `nested`,
  `sequence`), optional `DatabaseError`→503 branch.
- `netix_backend.django.pagination`, `serializers`, `filters`, `schema`, `excel`, `testing`.

### Migration notes

Replaces, per service: `service/models.py` base classes, `service/views.py` BaseViewSet copies,
`*/utils/custom_exception_handler.py`, `*/utils/http_retry.py`, `*/utils/static_service.py`,
env-bool helpers, and the `patch("<app>.views.EnvoyQueryFilter...")` test idiom. Defaults
preserve current behavior; deliberate fixes must be opted into per service (see README).
