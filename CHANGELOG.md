# Changelog

## v1.4.0 — 2026-09-11

*Why* a change was made, alongside the *what*, *when* and *by whom* v1.3.0 already recorded.
Ported from `user-management` issue #840, which extended its own copy of the trail while the
promotion was in flight; only the generic half comes across — re-resolving actor names against a
repo's own user records stays in the repo that has them.

Backward compatible: every new parameter is optional, `ChangeActor()` still constructs with no
arguments and `reason` is declared **last** so positional construction keeps its old meaning. A
consumer that never mentions a reason behaves exactly as it does on v1.3.0 — with one observable
difference, below.

### Added

- `netix_backend.django.change_history`:
  - `ChangeActor.reason` (default `""`) and `ChangeActor.from_user(..., reason="")` — free text,
    stated per write, never inferred.
  - `reason_context(reason)` — a context manager that folds a reason onto the **already-bound**
    actor for one write and restores it afterwards. The actor is bound per request; only the
    reason varies per save, so this replaces rather than rebuilds it.
  - `actor_context(..., reason="")` for the non-request write paths.
  - `normalize_reason(value)` — trims, caps at `MAX_REASON_LENGTH`, collapses anything falsy to
    `""`, so a reason never reaches the trail as `null` or as untrimmed free text.
  - `reason_from_query(request)` — the reason a body-less write (`DELETE`) states in its query
    string. Reads `query_params`, falls back to `GET`, tolerates `None`; the request body is
    deliberately never parsed.
  - `REASON_FIELD` (`change_reason`) and `MAX_REASON_LENGTH` (`256`).
- `netix_backend.django.serializers`:
  - `ChangeHistorySerializerMixin.change_reason` — write-only, not required, blank allowed, capped
    at `MAX_REASON_LENGTH`. `validate()` pops it onto `self._change_reason` rather than leaving it
    in `validated_data`, where `ModelSerializer` would hand it to `Model(**validated_data)` and
    fail on a field the model does not have; `save()` then wraps `super().save()` in
    `reason_context` when one was stated.
  - `ChangeHistoryEntrySerializer.reason` — read-only, blank and null both allowed.
- `netix_backend.django.change_history_schema` — `REASON_PARAMETER`, the ready-made
  `OpenApiParameter` for a `destroy` action, plus `reason_parameter(description=…, name=…)` and
  `REASON_DESCRIPTION`, mirroring the `HISTORY_PARAMETER` trio.

### Changed

- **Every entry now carries a `reason` key**, empty when the write stated none. This is the one
  observable difference for a consumer that never mentions a reason: entries written from v1.4.0
  on have `"reason": ""` where v1.3.0's had no key at all. Nothing else about the entry moves, no
  migration is generated, and entries recorded before the key existed stay as they are —
  `ChangeHistoryEntrySerializer` serves those as `null`, which is how a client tells "no reason
  was asked for" from "none was given".

### Migration notes

- `user-management` deletes its local `REASON_FIELD`, `MAX_REASON_LENGTH`, `normalize_reason`,
  `reason_context` and `reason_from_query` from `base/utils/change_history.py`, the `reason` field
  and the `change_reason` / `validate()` / `save()` block from `base/serializers.py`, and
  `base/serializers.REASON_PARAMETER`. Re-point the imports:
  `base.utils.change_history` → `netix_backend.django.change_history`;
  `base.serializers.REASON_PARAMETER` → `netix_backend.django.change_history_schema`. The
  `reason` key `ChangeHistoryModel` writes is byte-identical, so no data migration is needed.
- `staff_management/utils/history_actors.py` and the `ResolvedHistoryActorsMixin` in
  `staff_management/views.py` stay in `user-management`: they re-resolve trail actors against that
  repo's own staff records, which the library has no model for.
- Binding a delete's reason stays the consumer's job, as binding the actor already was:
  `ChangeActor.from_user(request.user, source=SOURCE_API, reason=reason_from_query(request))` in
  the viewset's `initial()`, and `extend_schema(parameters=[REASON_PARAMETER])` on `destroy` to
  advertise it.
- A serializer whose `Meta.fields` is an explicit list must add `"change_reason"` to it for the
  field to be accepted; one on `fields = "__all__"` picks the mixin's declared fields up itself.

## v1.3.0 — 2026-09-11

The on-row change trail, promoted out of `user-management` (issue #815) so `cafm-backend` adopts
one implementation instead of a second fork. Behavior-preserving: this is the `user-management`
code, and the entries it writes are byte-identical. The one addition is a documented
`resolve_actor()` hook, so a repo that binds its actor somewhere other than `request.user`
subclasses the middleware rather than forking it.

### Added

- `netix_backend.django.change_history` — runtime only, no drf-spectacular import anywhere, so a
  repo without the `spectacular` extra can still mix the model in:
  - `ChangeHistoryModel`, an abstract mixin carrying a `change_history` JSONField. Declare
    `history_fields` (nothing is recorded while it is empty) and every save diffs those fields
    against the values the row was loaded with, appending one `created` / `updated` / `deleted` /
    `restored` entry. `from_db` takes the baseline snapshot at load time so the diff costs no extra
    query, `refresh_from_db` re-baselines it, a partial `update_fields` write records only what it
    actually persists (the rest stays pending for the save that does write it), choice fields carry
    `old_display` / `new_display`, and `history_max_entries` (default 50) caps the trail.
  - `ChangeActor` + the `contextvars` binding (`get_actor`, `set_actor`, `reset_actor`,
    `actor_context`). `ChangeActor.from_user()` is duck-typed on `pk` / `get_full_name()` /
    `username` — nothing here imports `django.contrib.auth`, and a non-integer `pk` degrades to
    `user_id=None` rather than writing an unqueryable id into the trail.
  - `ChangeHistoryActorMiddleware` (sync + async), which **resets** the actor around each request
    and binds nobody by default. Overriding `resolve_actor(request) -> ChangeActor | None` is the
    supported way to bind from something the middleware layer can already see (an Envoy identity);
    with no override its behaviour is exactly user-management's of today.
  - `json_safe()`, `history_requested()`, `HISTORY_QUERY_PARAM` (`include_history`),
    `TRUTHY_PARAM_VALUES`, and the `SOURCE_API` / `SOURCE_UPLOAD` / `SOURCE_SYSTEM` /
    `SOURCE_MOBILE` constants.
- `netix_backend.django.serializers` gained `ChangeHistorySerializerMixin`,
  `ChangeHistoryEntrySerializer` and `ChangeHistoryChangeSerializer`. The mixin drops
  `change_history` in `__init__` (not after `to_representation`, which would serialize the whole
  trail and then throw it away) for any request that did not pass `?include_history=true`, and
  keeps the field declared for schema generation (`swagger_fake_view`).
- `netix_backend.django.change_history_schema` — `HISTORY_PARAMETER` (the ready-made
  `OpenApiParameter`, same prose as today), `history_parameter(description=…, name=…)` for a repo
  that wants its own, and `HISTORY_DESCRIPTION`. Split from the runtime module for the same reason
  `org_scope_schema` is split from `org_scope`: drf-spectacular stays optional.

### Migration notes

- Adopting repos delete `base/models.py`'s `ChangeHistoryModel` + `json_safe`,
  `base/utils/change_history.py`, `base/middleware/change_history.py` and the change-history block
  of `base/serializers.py`, then re-point their imports:
  `base.utils.change_history` → `netix_backend.django.change_history`;
  `base.serializers` (`ChangeHistorySerializerMixin`) → `netix_backend.django.serializers`;
  `base.serializers.HISTORY_PARAMETER` → `netix_backend.django.change_history_schema`. The
  `MIDDLEWARE` entry becomes
  `netix_backend.django.change_history.ChangeHistoryActorMiddleware`. No migration is generated:
  the column, its default and its `help_text` are unchanged.
- **Binding the actor is now explicitly the consumer's job**, because it always was: the
  middleware never bound one. user-management keeps binding from `request.user` in its
  `BaseViewSet.initial()`; a repo behind Envoy with no Django user subclasses the middleware and
  overrides `resolve_actor()` instead of rewriting it.
- `ChangeHistoryEntrySerializer.by`'s help text now reads "Acting user id" rather than naming
  `OrganizationUser`, since the library has no such model. It is prose in the generated schema
  only; regenerate the OpenAPI contract when adopting.

## v1.2.0 — 2026-08-25

Third wave: the Tier-1/Tier-3 reuse candidates, verified by a six-survey research pass over the
18 services. Everything ships behavior-preserving; per-repo call sites reproduce today's
settings byte-identical.

### Added

- `netix_backend.database` (Django-free; also importable as `netix_backend.django.database`) —
  `postgres_database(...)` and `replica_of(...)` DATABASES factories with `FromEnv` (raw
  `os.environ` semantics: an empty string stays an empty string) and the `OMIT` / `REQUIRED`
  sentinels (now homed in `netix_backend.env`). `OPTIONS` is emitted only when a member is
  set, so the fleet's 9-present / 9-absent `prepare_threshold` split survives adoption
  unchanged; flipping it is a visible one-argument diff per repo.
- `netix_backend.observability.sentry.configure_sentry(...)` — the `sentry_sdk.init`
  boilerplate 17 services repeat. `environment` is required with no default (a hardcoded
  "main" stays a visible argument; unification remains a separate reviewable change);
  `enabled` is computed at the call site so all five fleet `SENTRY_ENABLED` spellings survive
  verbatim; `sentry_sdk` is imported only inside the function; an empty `dsn` passes through
  (simulator-service relies on that path); `traces_sample_rate=OMIT` omits the kwarg
  (user-management), `logging_event_level=None` sends `LoggingIntegration(event_level=None)`
  (backend-template).
- `netix_backend.django.test_settings` — the two-phase test-settings bootstrap:
  `apply_test_env(force=..., exclude=..., **overrides)` (applies, unlike v1.1.0's dead
  `netix_test_settings()`; `force=` for cafm's hard-set Redis keys, `exclude=` so
  user-management's `DJANGO_SECRET`-derived MFA keys stay untouched), `load_base_settings()`,
  `test_overrides(...)` and `EnvoySpec`. `TEST_ENV_DEFAULTS` gained
  `CSRF_TRUSTED_ORIGINS="http://localhost:8000"`.
- Test-double kit in `netix_backend.django.testing`: `client_response()` (+
  `client_response_factory` fixture) replacing the DummyResponse stubs nine repos hand-roll,
  `envoy_api_client()` (+ `envoy_client` fixture), `block_http()` (+ `no_unmocked_http`
  fixture), `clear_envoy_cache`, `envoy_request_factory`. None autouse. The test middlewares
  now honor a `NETIX_TEST_ENVOY_HEADER_GUARD` setting (default True), so the two repos pinning
  `header_guard = False` can delete their subclass files.
- `netix_backend.django.org_scope.SuperuserOrgScopeMixin` — the superuser `?organization=<id>`
  cross-org mechanism four repos fork, runtime only: the mixin never defines `schema`, never
  imports drf-spectacular, never emits a parameter. Knobs cover every measured divergence
  (blank-param handling, invalid-message text, scope field, ordering, extra filter) and both
  entry surfaces ship (`get_queryset` and `get_org_scoped_queryset` for cafm's shape). The
  four advertising helpers live in `netix_backend.django.org_scope_schema`
  (`..._parameter` / `..._schema` / `..._autoschema` / `..._parameter_dict`); `description`
  is required on all four so the library can never rewrite a repo's contract prose.
- `prospector_profile_netix` — a second package in the same wheel carrying the shared
  prospector profile; consume with `inherits: [netix]` (Django variant: `[netix:django]`).
  The top-level `max-line-length: 120` is the only key that also raises pylint's threshold;
  `test-warnings` is deliberately unset (setting it strips prospector's default
  test-exclusion patterns). This library's own prospector.yaml now inherits it.

### Migration notes

- The pin bump to v1.2.0 and a repo's `prospector.yaml` switch to `inherits: [netix]` must
  land in the same commit — inheriting against an installed v1.1.0 is a hard prospector
  failure.
- `postgres_database` / `configure_sentry` adoption calls are catalogued per repo in the
  research spec; dict contents are identical but key order is canonical (`key_order=` is the
  escape hatch for repr-snapshot tests).
- vision-ai-service adopts the database factory with three explicit `OMIT`s and has no Sentry.

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
