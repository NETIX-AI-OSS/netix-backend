# v1.2.0 notes — proposed CHANGELOG bullets and README sections

Scratch file for the release PR. CHANGELOG.md and README.md are owned by other v1.2.0 work in
flight, so the prose lands here first and is merged by whoever cuts the tag.

## Proposed CHANGELOG bullets

```markdown
### Added

- `netix_backend.django.org_scope.SuperuserOrgScopeMixin` — cross-organization reads and writes for
  platform superusers via `?organization=<id>`, replacing four independent forks (asset-service,
  cafm-backend, data-service, visualization-backend). Runtime only: the mixin never defines `schema`,
  never imports drf-spectacular and never emits a parameter, because the four repos advertise it
  through three different mechanisms and visualization-backend is not on drf-spectacular at all.
  Seven per-repo divergences are class-attribute knobs: `superuser_org_query_param`,
  `superuser_org_scope_field`, `superuser_org_scope_model`, `superuser_org_blank_is_absent`,
  `superuser_org_invalid_message`, `superuser_org_forbidden_message`, `superuser_org_ordering`,
  `superuser_org_extra_filter`. Both entry surfaces ship: the mixin overrides `get_queryset`, and
  `get_org_scoped_queryset(model, **scope_kwargs)` is callable from a viewset that owns its own.
  Three behaviours are normalized rather than parameterized: the request-less schema pass is guarded
  (data-service and visualization-backend crash there today), the cross-org read goes through
  `_default_manager` rather than `.objects`, and `perform_create` / `perform_update` return the
  save result.
- `netix_backend.django.org_scope_schema` — the four advertising helpers, each with all
  drf-spectacular imports function-local: `superuser_org_scope_parameter()` (the bare
  `OpenApiParameter`), `superuser_org_scope_schema()` (the `extend_schema_view` class decorator,
  defaulting to the six CRUD actions), `superuser_org_scope_autoschema()` (an `AutoSchema` subclass
  overriding `get_override_parameters`), and `superuser_org_scope_parameter_dict()` (the raw
  `name`/`required`/`in`/`description`/`schema` dict for DRF-native `generateschema`, which imports
  nothing). `description` is required with no default on all four: the library must never be able to
  silently rewrite an adopter's published prose.
- `prospector_profile_netix` — the shared prospector profile, shipped in the same wheel and consumed
  as `inherits: [netix]` (plus an opt-in `inherits: [netix:django]`). Two traps it encodes:
  `test-warnings` is deliberately left unset, because setting it strips prospector's default
  test-exclusion `ignore-patterns` and starts linting every adopter's test suite; and the
  **top-level** `max-line-length: 120` is the only key that also raises pylint's `line-too-long`
  threshold off its `strictness: high` default of 99 — `pycodestyle.options.max-line-length` does
  not reach pylint. The library self-hosts the profile, so its own CI gate proves the mechanism.
```

## Proposed README sections

Two rows for the layout table:

```markdown
| `netix_backend.django.org_scope` | `SuperuserOrgScopeMixin`, `ORGANIZATION_QUERY_PARAM`, `INVALID_ORGANIZATION`, `FORBIDDEN_ORGANIZATION` — cross-org `?organization=<id>` for superusers, runtime only |
| `netix_backend.django.org_scope_schema` | `superuser_org_scope_parameter` / `_schema` / `_autoschema` / `_parameter_dict` — the OpenAPI advertising for it (the first three need the `spectacular` extra) |
```

New prose section, after the `BaseViewSet` paragraph:

```markdown
## Cross-organization access for superusers

`SuperuserOrgScopeMixin` lets a platform superuser read and write another organization's rows with
`?organization=<id>`. Without the parameter nothing changes; with it, reads go to the target
organization's live rows and writes are pinned to it regardless of the body. A non-superuser that
sends the parameter gets 403 — including one naming its own organization — and a malformed value is
a 400 for everyone, because the parse happens before the gate.

```python
from netix_backend.django.org_scope import SuperuserOrgScopeMixin

class OrganizationConfigViewSet(SuperuserOrgScopeMixin, BaseViewSet):
    superuser_org_scope_model = OrganizationConfig
```

A viewset that owns its own `get_queryset` calls the helper instead of inheriting the override:

```python
    def get_queryset(self):
        return self.get_org_scoped_queryset(OrganizationConfig, field_name="organization_id")
```

Knobs, all class attributes: `superuser_org_query_param` (`"organization"`),
`superuser_org_scope_field` (`"organization_id"`), `superuser_org_scope_model` (falls back to
`model_queryset` / `queryset` / the serializer's model, then `RuntimeError`),
`superuser_org_blank_is_absent` (`False` — a blank `?organization=` is a 400; set `True` to read it
as absent), `superuser_org_invalid_message`, `superuser_org_forbidden_message`,
`superuser_org_ordering` (`("id",)`) and `superuser_org_extra_filter` (`{"is_deleted": False}` —
load-bearing, since `BaseManager.get_queryset` does not filter soft-deleted rows). Override
`superuser_org_manager()` to read through something other than `_default_manager`.

On the targeted branch the write path calls `serializer.save()` directly instead of `super()`: the
query parameter is meant to win over the body, so a `tenant_write_scope="pin_and_reject"` below this
mixin never sees the cross-org write and never rejects it. That asymmetry is deliberate and inherited.

**The mixin advertises nothing.** It never defines `schema`, never imports drf-spectacular and never
emits a parameter — three of the four adopting repos use different mechanisms and one generates its
contract with DRF's own `generateschema`. Pick the matching helper from
`netix_backend.django.org_scope_schema` and pass your existing description verbatim:

| Repo shape | Helper |
|---|---|
| `@extend_schema_view(...)` written out by hand | `superuser_org_scope_parameter(description=...)` |
| a decorator factory over the six CRUD actions | `superuser_org_scope_schema(description=...)` |
| `schema = SomeAutoSchema()` bound on the viewset | `superuser_org_scope_autoschema(description=...)` |
| DRF-native `AutoSchema.get_operation` injection | `superuser_org_scope_parameter_dict(description=...)` |

`description` is required and has no default on all four, so adopting the library cannot change one
byte of a published contract.
```

New prose section, near the end:

```markdown
## Shared prospector profile

The wheel also ships `prospector_profile_netix`, so every repo that already depends on
`netix-backend` can replace its prospector config with an inherit:

```yaml
# prospector.yaml
inherits:
  - netix

ignore-paths:
  - service/migrations
  - clients
```

`inherits: [netix:django]` adds `uses: [django]` and the two pylint disables that Django viewsets
need; treat it as opt-in per repo, since `pylint_django` surfaces new messages in repos that do not
set it today. Inherited lists (`ignore-paths`, every `disable`) union with the local ones; scalar
options (`max-line-length`, `max-complexity`) are overridden by the local value.

Two things the profile encodes deliberately:

- **`test-warnings` is unset.** Setting it strips prospector's default test-exclusion
  `ignore-patterns`, which starts linting every test file at `strictness: high`. No fleet repo does
  this today. If a repo wants it, it goes in that repo's own file — this library's does.
- **`max-line-length: 120` is top-level, not just under `pycodestyle`.** It is the only key that also
  raises pylint's `line-too-long` threshold off its `strictness: high` default of 99, which is why 14
  of the fleet's pre-commit hooks pass `--max-line-length 120` on the command line. Once the profile
  is inherited that flag is redundant, as is `--profile-path ./prospector.yaml` (it takes search
  *directories*, so passing a file is a no-op).
```

## Decisions worth flagging in review

- **H2, `?organization=0`, left permissive.** A superuser can still read and write the platform
  organization's rows through a config endpoint. All four repos do that today, so no
  `superuser_org_allow_platform_org` knob ships in v1.2.0 — tightening it would be a behaviour change
  smuggled into a dedup. It deserves its own ticket.
- **H4 not fixed here.** Three of the four repo descriptions claim a non-superuser is only rejected
  "for an org other than its own"; the code rejects any non-superuser that sends the parameter at
  all. Because `description` is passed in verbatim by the adopter, adoption changes nothing — fixing
  the prose is a deliberate, separate schema change with three frontends downstream of it.
- **H5, user-management's `LicenseStatusView`, is out of scope.** It is a plain `APIView` that returns
  `Response` objects instead of raising, and gates on `request.user.is_superuser` only — an
  Envoy-authenticated superuser is refused there and accepted in the other four. That is an
  authorization bug, not an adoption target.
