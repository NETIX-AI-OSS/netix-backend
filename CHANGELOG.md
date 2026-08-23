# Changelog

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
