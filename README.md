# netix-backend

Shared Django/DRF base layer for NETIX backend services: soft-delete models, tenant-scoped
viewsets, the `{"status_code": ..., "messages": ...}` error envelope, limit/offset pagination,
HTTP retry transports, a generated-client factory, static-service uploads, and env parsing.

One package, one version. Import only what you need — an unused subpackage costs nothing.

## Install

```toml
# pyproject.toml
dependencies = [
    "netix-backend @ git+https://github.com/NETIX-AI-OSS/netix-backend.git@v1.0.0",
]
```

Extras: `[spectacular]` (OpenAPI schema helpers), `[async]` (adrf viewsets), `[excel]` (xlsx export).

## Layout

| Module | Contents |
|---|---|
| `netix_backend.env` | `env_bool`, `env_bool_strict`, `env_str`, `env_int`, `env_float`, `env_first`, `service_authorization` — importable without Django configured |
| `netix_backend.http` | `RetryTransport` / `AsyncRetryTransport`, `build_client`, `post_with_retry`, `static_upload` / `static_fetch`, healthz helpers |
| `netix_backend.django.models` | `BaseModel`, `NamedBaseModel`, `SluggedNamedBaseModel`, `CompactNamedBaseModel`, `BaseManager`, `CloneProvenanceMixin`, `organization_scoped()` |
| `netix_backend.django.views` | `BaseViewSet` and its mixins (scoping, tenant write pinning, atomic writes, soft delete, permissions) |
| `netix_backend.django.views_aio` | `AsyncBaseViewSet` (requires the `async` extra) |
| `netix_backend.django.exceptions` | `custom_exception_handler` + message flatteners, behind `NETIX_ERRORS_*` settings |
| `netix_backend.django.pagination` | `limit_offset_pagination()` factory, `BaseLimitOffsetPagination`, `BigLimitOffsetPagination` |
| `netix_backend.django.serializers` | `BASE_FIELDS`, `NAMED_BASE_FIELDS`, `NamedBaseSerializer` |
| `netix_backend.django.filters` | `CharArrayFilter`, `NumberInFilter`, `IntArrayFilter` |
| `netix_backend.django.schema` | `AsyncActionAutoSchema`, `XlsxExportAutoSchema`, `XLSX_BINARY_RESPONSE` (requires the `spectacular` extra) |
| `netix_backend.django.excel` | `BaseExcelViewSet` + style presets (requires the `excel` extra) |
| `netix_backend.django.testing` | `unscoped_envoy` / `explicit_envoy_identity` pytest fixtures, `netix_test_settings()` |

Behavioral knobs default to what the fleet ships today; behavior changes are opt-in per service.
See `CHANGELOG.md` for migration notes per release.

## Development

```bash
uv sync --all-groups --all-extras
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy netix_backend
```

Releases are annotated 3-component tags (`vX.Y.Z`) matching `[project].version`; tags are
immutable via repo ruleset. Consumers are kept current by `netix deps check` in the
NETIX-AI/monorepo workspace tooling.
