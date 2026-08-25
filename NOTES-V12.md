# v1.2.0 — proposed CHANGELOG and README copy (databases + sentry)

Staging file for the release commit. `CHANGELOG.md` and `README.md` are owned by the release
branch, so the text lives here until the v1.2.0 merge folds it in. Delete this file then.

## Proposed `CHANGELOG.md` bullets — under `## v1.2.0` → `### Added`

- `netix_backend.database` — the `DATABASES` factory the 18 services hand-roll
  (`postgres_database()`, `replica_of()`, `FromEnv`, `OMIT`, `REQUIRED`, `POSTGRES_ENGINE`,
  `POSTGIS_ENGINE`). Pure stdlib and Django-free, because `app/settings.py` builds `DATABASES`
  long before Django is configured; re-exported as `netix_backend.django.database` so a settings
  module can import it from either path.
  - Every knob takes a literal, a `FromEnv` to read at settings-import time, or `OMIT` to leave
    the key out of the alias entirely. `OMIT` is not `None` and not `False`: it is the only way
    to reproduce a key that is *absent* today.
  - `prepare_threshold` defaults to `OMIT`, so the nine services with no `OPTIONS` key keep having
    none and the nine that set `{"prepare_threshold": None}` pass it explicitly. `OPTIONS` is only
    emitted once one of `prepare_threshold` / `connect_timeout` / `options` is set. Flipping the
    fleet to a uniform value stays a separate diff that shows up at nine call sites.
  - `conn_max_age=OMIT` covers notification-service and static-service, which omit the key
    (Django's default is 0, so this is cosmetic — but it keeps the adoption diff empty).
  - `FromEnv` reads **raw `os.environ`**, never `netix_backend.env._lookup`. `_lookup` maps `""`
    and whitespace to `None`, which would make an empty `POSTGRES_HOST` start resolving to
    `historian.platform` in the six services that pass a host default — a connect to the wrong box
    instead of a loud connect failure. A `REQUIRED` default reproduces `os.environ[var]` including
    its `KeyError`; any other default reproduces `os.environ.get(var, default)`.
  - `replica_of()` is the `{**DATABASES["default"], "HOST": …, "PORT": …}` idiom, preserving the
    primary's key order. In a copied alias `OMIT` *drops* the inherited key rather than skipping
    the write — that is the only sensible reading of "this key must not be here".
- `netix_backend.observability.sentry.configure_sentry()` — one call for the `sentry_sdk.init`
  block 17 services copy, plus the `set_tag` and `ignore_logger` tail. Returns whether init ran.
  - `sentry_sdk` is imported **inside** the function. Every service imports it inside its own
    `if SENTRY_ENABLED:` today, so a module-level import would newly load the SDK in every
    disabled and test process. sentry-sdk is deliberately **not** a dependency of this library,
    not even a dev one — the tests simulate it through `sys.modules`.
  - `enabled` is computed at the call site, so all five `SENTRY_ENABLED` spellings (three strict
    `os.environ[...] == "TRUE"` conjunctions, the `.get(..., "FALSE")` form, and the two
    `env_bool()` ones) survive verbatim.
  - `environment` is a required keyword with **no default**: the helper must never invent an
    environment. A hardcoded `"main"` stays visible as `environment="main"` in the diff, and the
    eventual unification stays a separate reviewable change across 17 call sites.
  - `traces_sample_rate=OMIT` exists for user-management, which omits the kwarg — the SDK reads an
    absent rate as tracing-off and `0` as tracing-on at 0% sampling, so they are not
    interchangeable. `before_send=OMIT` exists for backend-template, which omits it.
  - `logging_event_level` is `OMIT`-vs-`None`, not truthy: `OMIT` means no `LoggingIntegration`,
    `None` means `LoggingIntegration(event_level=None)`, which is what backend-template needs.
  - An empty `dsn` is passed straight through and never short-circuited: simulator-service inits
    with `os.environ.get("SENTRY_URL", "")` and still relies on the `ignore_logger` tail running.
- `netix_backend.env` gains the shared `OMIT` / `REQUIRED` sentinels both settings factories use,
  so there is one sentinel identity in the package rather than one per module.

### Notes for adopters

- The factory emits one canonical key order (`ENGINE`, `NAME`, `USER`, `PASSWORD`, `HOST`, `PORT`,
  `CONN_MAX_AGE`, `CONN_HEALTH_CHECKS`, `ATOMIC_REQUESTS`, `DISABLE_SERVER_SIDE_CURSORS`,
  `OPTIONS`, `TEST`). Every service orders its alias differently today. Django reads `DATABASES`
  by key so this is inert, but a test that snapshots `repr(DATABASES["default"])` will fail —
  pass `key_order=` to pin it.
- asset-service and simulator-service bind one `_DB_OPTIONS` dict and reference the same object
  from both aliases; the factory produces a fresh `OPTIONS` dict per alias. Nothing mutates it at
  runtime today, and Django's `ConnectionHandler` copies `settings_dict` anyway.
- All 18 services replace `DATABASES` wholesale in test settings and 13 force Sentry off, so
  neither helper gets meaningful coverage from an existing suite. Adoption PRs need a direct unit
  test asserting the produced dict equals a literal copy of today's dict — `tests/test_database.py`
  here is the template.
- ml-engine must keep instantiating `OpenAIIntegration()` inside its existing `if SENTRY_ENABLED:`
  wrapper. Hoisting that import to module scope would newly probe for `openai` in every process.
- visualization-backend's nested `if SENTRY_URL:` folds into `enabled=SENTRY_ENABLED and
  bool(SENTRY_URL)`. That also skips the SDK imports it performs today — no observable difference,
  but it belongs in the PR body.

## Proposed `README.md` changes

Two rows for the Layout table, placed after the `netix_backend.cloning` row and after the
`netix_backend.observability.sentry_filters` row respectively:

```
| `netix_backend.database` | `postgres_database()`, `replica_of()`, `FromEnv`, `OMIT` — the `DATABASES` factory; re-exported as `netix_backend.django.database`, importable without Django configured |
| `netix_backend.observability.sentry` | `configure_sentry()` — one call for `sentry_sdk.init` plus the `set_tag` / `ignore_logger` tail; imports the SDK lazily and never depends on it |
```

New section, after `## Statement timeout` and before `## Cache settings`:

````markdown
## Database settings

```python
# app/settings.py
from netix_backend.database import OMIT, FromEnv, postgres_database, replica_of

DATABASES = {
    "default": postgres_database(
        host=FromEnv("POSTGRES_HOST", "historian.platform"),
        port=FromEnv("POSTGRES_PORT", "5432"),
        conn_max_age=int(os.environ.get("CONN_MAX_AGE", "60")),
        conn_health_checks=True,
        prepare_threshold=None,
        connect_timeout=int(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "5")),
    )
}
DATABASES["read_replica"] = replica_of(
    DATABASES["default"],
    host=os.environ.get("POSTGRES_READ_HOST", "historian.platform"),
    port=os.environ.get("POSTGRES_READ_PORT", "5433"),
)
```

Every knob takes a literal, a `FromEnv` to read at settings-import time, or `OMIT` to leave the
key out of the alias. `OMIT` is not `None` and not `False` — `prepare_threshold=OMIT` (the
default) produces no `OPTIONS` key at all, while `prepare_threshold=None` produces
`{"prepare_threshold": None}`. `FromEnv` reads raw `os.environ`, so an empty `POSTGRES_HOST` stays
`""` instead of silently falling through to the host default; a `REQUIRED` default (the default)
reproduces `os.environ[var]`, `KeyError` included.

Django reads `DATABASES` by key, so the emitted key order is inert — pass `key_order=` only if a
test snapshots the dict's repr.
````

New section, after `## Observability`:

````markdown
## Sentry

```python
# app/settings.py
from netix_backend.observability.sentry import configure_sentry

SENTRY_ENABLED = os.environ["SENTRY_ENABLED"] == "TRUE"
configure_sentry(
    enabled=SENTRY_ENABLED,
    dsn=os.environ["SENTRY_URL"],
    environment="main",
    before_send=before_send,
    ignore_loggers=IGNORED_TELEMETRY_LOGGERS,
)
```

`sentry_sdk` is imported inside the call, so a disabled service never loads it and the library
never depends on it. `enabled` is computed at the call site — keep whatever spelling of
`SENTRY_ENABLED` your service uses, including the `IS_TEST_MODE` / `IS_INTERACTIVE_SHELL`
conjunctions. `environment` is required and has no default: the helper will not invent one.
`traces_sample_rate` defaults to `0` and takes `OMIT` to omit the kwarg entirely (an absent rate
is tracing-off; `0` is tracing-on at 0% sampling). `logging_event_level=None` adds
`LoggingIntegration(event_level=None)`; the default `OMIT` adds no logging integration. An empty
`dsn` is passed through untouched.
````

## Follow-ups deliberately not taken here

1. `netix_backend/observability/__init__.py` is not in this change's file scope, so
   `configure_sentry` is not re-exported from the subpackage yet. Add
   `from netix_backend.observability.sentry import configure_sentry` to the `__init__` and to its
   `__all__` in the release commit; the call sites in the spec import from the module directly and
   keep working either way.
2. `pyproject.toml` is not in scope, so the three unavoidable
   `# type: ignore[import-not-found]` comments in `observability/sentry.py` stand in for a
   `[[tool.mypy.overrides]] module = ["sentry_sdk.*"] ignore_missing_imports = true` entry. mypy
   reports each missing module once, at its *first* import site, so the ignores are positional and
   `warn_unused_ignores` will fail loudly if the import order in that module changes. Adding the
   override is the durable fix and does not make sentry-sdk a dependency.
3. `FromEnv(var)` raises `KeyError`, not `ConfigurationError`, where
   `netix_backend.django.cache._env` raises `ConfigurationError`. The spec is explicit that a
   `REQUIRED` default reproduces `os.environ[var]`, and every service crashes boot with a bare
   `KeyError: 'POSTGRES_NAME'` today, so adoption stays behaviour-free. Harmonising the two
   modules on `ConfigurationError` is a worthwhile v1.3 change — but it is a behaviour change to
   18 boot paths and belongs in its own diff.
4. Nothing in the DB module knows about `DATABASE_ROUTERS` or the seven services' replica routers.
   asset-service's `read_replica` is genuinely conditional on both `POSTGRES_READ_HOST` and
   `POSTGRES_READ_PORT` being non-empty; cafm-backend's and user-management's guards are not
   conditional in practice. Do not harmonise those three into one helper shape — cafm's
   `get_read_db()` fallback depends on the alias being legitimately absent.
5. `before_send` composition stays caller-supplied. Eleven services are
   `chain(drop_cancelled_errors)` or the bare filter; six compose something bespoke. On the
   license-server question raised by the survey: `chain(f)` is **not** an identity wrapper for `f`.
   `sentry_filters.chain` catches a raising filter and keeps the event, so license-server's bare
   `drop_cancelled_errors` would drop every event if it ever raised, where the ten `chain(...)`
   services would not. That is an argument for wrapping license-server too — separately, since it
   is a behaviour change.
6. data-service and visualization-backend define local copies of `group_log_events_by_template`
   and friends rather than importing `netix_backend.observability.sentry_filters`. data-service's
   version appends the exception type to the fingerprint. Diff them before consolidating.

# v1.2.0 — test-settings + test-double kit (branch `v12-testkit`)

Proposed CHANGELOG bullets and README sections for the `netix_backend.django.test_settings`
module and the `netix_backend.django.testing` additions. Nothing here is applied to
`CHANGELOG.md` / `README.md` — those files are owned by the release commit.

---

## Proposed CHANGELOG bullets

### Added

- `netix_backend.django.test_settings` — the two-phase test-settings recipe all 18
  `app/settings_test.py` / `app/test_settings.py` modules hand-roll. Import-light on purpose:
  no `django.conf` at module scope, because phase 1 runs before the base settings exist.
  - `apply_test_env(*, force=None, exclude=(), **overrides)` — `os.environ.setdefault`s the
    shared baseline plus the caller's overrides, then assigns `force` unconditionally, and
    returns the *resulting* environment for every key it managed (so a settings module can
    assert on it). `force=` exists for cafm-backend, which pins Redis/Sentinel hosts because an
    inherited unreachable `REDIS_HOST` hangs Celery's `.delay()`; a `setdefault`-only helper
    would silently regress that on any dev machine with the variable exported. `exclude=` is the
    opposite escape hatch, for user-management, which derives `MFA_SECRET_ENCRYPTION_KEY` and
    `MFA_OTP_HASH_KEY` from `DJANGO_SECRET` and must be able to leave that key unset. `exclude`
    wins over `force`.
  - `load_base_settings(namespace, module="app.settings", *, skip_existing=False)` —
    `import_module` + uppercase copy into the namespace, returning the module.
    `skip_existing=True` reproduces asset-service's `not in globals()` guard. Repos that
    already write `from .settings import *` keep doing that and hand the module they imported
    straight to `test_overrides()`.
  - `test_overrides(base=None, *, database, engine, aliases, alias_mirror, clear_routers,
    atomic_requests, cache_location, session_backend, fast_hashers, disable_migrations,
    static_root, email_locmem, celery_eager, quiet_db_logging, envoy)` — the uppercase override
    dict to splat into the settings namespace after the base import.
  - `EnvoySpec(permissions, username, bearer, mode, middleware, identity)` — how a suite obtains
    `request.envoy`. `mode="resolver"` (default) swaps in `EnvoyTestAuthorizationMiddleware`,
    `"explicit"` swaps in `ExplicitEnvoyIdentityMiddleware`, `"none"` keeps the production
    middleware and only emits the identity/bearer settings (report-engine mirrors prod).
  - `DisableMigrations`, `quiet_db_logging_config()`, `SQLITE_ENGINE`, `LOCMEM_CACHE_BACKEND`,
    `LOCMEM_EMAIL_BACKEND`, `MD5_PASSWORD_HASHER`, `SESSION_ENGINES`, `MODE_MIDDLEWARE`,
    `CELERY_EAGER_SETTINGS`.
- `netix_backend.django.testing` test doubles, each duplicated in 3–9 repos today:
  - `client_response(status_code=200, *, parsed, content, text, results, count)` and the
    `ClientResponse` class behind it — the generated-client `Response` stub nine repos
    hand-roll, modelled on notification-service's `DummyResponse` (the most complete copy).
    `count` defaults to `len(results)`; extra keyword fields are kept as attributes, because the
    nine copies each stub a slightly different surface. Fixture: `client_response_factory`.
  - `envoy_api_client(monkeypatch, *, identity=None, permissions=(), bearer=None,
    mode="resolver"|"handler")` — the authenticated `APIClient` six repos write six ways.
    `mode="handler"` is ml-engine's seam (wrap the test client's `get_response` and stamp
    `request.envoy`), for suites whose `MIDDLEWARE` carries no Envoy middleware at all.
    Fixture: `envoy_client`, parameterised by `netix_envoy_identity`.
  - `block_http(monkeypatch, *, allow=())` and `UnmockedHTTPCall` — cafm-backend's transport
    guard, sync *and* async, with an actionable message naming the method and URL. `allow` holds
    substrings of the request URL that reach the real transport. Fixture: `no_unmocked_http`
    (not autouse; re-declare it autouse in the repo conftest that wants it everywhere).
  - `clear_envoy_cache` fixture — drops envoy-pyauth's positive `/auth/me/` cache on entry and
    on exit. Not autouse: it clears the whole default alias.
  - `envoy_request_factory` fixture — an `APIRequestFactory` that stamps `request.envoy` and
    `HTTP_AUTHORIZATION`, since the factory bypasses middleware. `APIRequestFactory` appears in
    13 of 18 repos' test trees; asset-service and cafm-backend monkeypatch the DRF class
    globally to get this.
- `CSRF_TRUSTED_ORIGINS="http://localhost:8000"` joined `TEST_ENV_DEFAULTS`. Six repos set it
  with five different values and none could inherit it; the four with a different value pin it
  locally via `apply_test_env(CSRF_TRUSTED_ORIGINS=…)`.
- `NETIX_TEST_ENVOY_HEADER_GUARD` setting (default `True`, read by `configured_header_guard()`).
  Either the class attribute or the setting turns the guard off, so update-service and
  stormbreaker can delete their `app/test_auth_middleware.py` subclass files.

### Changed

- `netix_envoy_identity` now returns `configured_identity()` — `NETIX_TEST_ENVOY_IDENTITY` if
  the repo declares it, else the minimal default it returned in v1.1.0. This makes the fixture
  side and the settings side of a repo name one identity; it is inert for `EnvoyQueryFilter`,
  which reads only `organization` / `is_superuser`, and it is what `envoy_client` and
  `envoy_request_factory` build on. Only visualization-backend uses `explicit_envoy_identity`
  *and* declares the setting, and both of its identities carry `organization: 0`.
- `sqlite_databases()` is now a thin wrapper over `test_overrides(database=…)`; the returned
  shape is unchanged. `netix_test_settings()` is unchanged apart from picking up the new
  `CSRF_TRUSTED_ORIGINS` key. Neither is deleted — consumers pin exact tags.

---

## Proposed README sections

### Layout table rows

| `netix_backend.django.test_settings` | `apply_test_env()`, `load_base_settings()`, `test_overrides()`, `EnvoySpec`, `DisableMigrations` — the settings-time half of a test settings module, importable before `django.setup()` |
| `netix_backend.django.testing` | `envoy_client` / `envoy_request_factory` / `no_unmocked_http` / `clear_envoy_cache` / `client_response_factory` / `unscoped_envoy` / `explicit_envoy_identity` fixtures, `client_response()`, `envoy_api_client()`, `block_http()`, `swap_auth_middleware()`, `platform_test_identity()`, the two test middlewares |

### Test settings

```python
# app/settings_test.py
from netix_backend.django.test_settings import EnvoySpec, apply_test_env, test_overrides

apply_test_env(DJANGO_SECRET="tag-service-test-secret")

from . import settings as base_settings  # noqa: E402
from .settings import *  # noqa: E402,F401,F403
from .settings import BASE_DIR  # noqa: E402

globals().update(
    test_overrides(
        base_settings,
        database=BASE_DIR / "test.sqlite3",
        aliases=("readonly",),
        cache_location="tag-tests",
        session_backend="signed_cookies",
        disable_migrations=["tag"],
        envoy=EnvoySpec(permissions=["tag-view", "tag-edit"], username="tag-service-test-platform"),
    )
)
```

Two calls, because the env must precede the base settings import and the overrides must follow
it. `apply_test_env()` setdefaults; pass `force={...}` for a key whose inherited value is
actively harmful, and `exclude=("DJANGO_SECRET",)` for a key the service must leave unset.
A standalone test settings module (no `app.settings` to import) calls
`test_overrides(None, …)` and gets everything except the `MIDDLEWARE` swap.

### Test doubles

```python
# service/tests/conftest.py
import pytest
from netix_backend.django.testing import block_http, client_response  # noqa: F401


@pytest.fixture
def netix_envoy_identity():
    return {"organization": 1, "is_superuser": False, "permissions": ["gateway-view"]}


@pytest.fixture(autouse=True)
def _no_unmocked_http(no_unmocked_http):
    """Opt the whole suite into the transport guard."""
```

`envoy_client` is an `APIClient` whose every request resolves to `netix_envoy_identity`;
`envoy_request_factory` is an `APIRequestFactory` that stamps the same identity for direct view
calls. `client_response(200, results=[...])` replaces the per-repo `DummyResponse`.
`block_http(monkeypatch, allow=("localhost",))` fails a test that reaches httpx' real transport.

---

## Per-repo adoption notes

### The three hazard repos

- **user-management** — must call `apply_test_env(exclude=("DJANGO_SECRET",), ...)`. Its
  `MFA_SECRET_ENCRYPTION_KEY = b64(sha256(os.environ.get("DJANGO_SECRET", "test-secret")))` and
  `MFA_OTP_HASH_KEY = DJANGO_SECRET + "-otp"` change value if the library's
  `test-secret-key-not-for-production` lands. Also keeps `SENTINEL_SERVICE="test-service"`, the
  `TEST_DATABASE` switch and the ArrayField block local; `disable_migrations=True` replaces its
  own `class DisableMigrations`.
- **cafm-backend** — `apply_test_env(DJANGO_SECRET="cafm-backend-test-secret",
  CSRF_TRUSTED_ORIGINS="http://localhost", force={"REDIS_HOST": …, "SENTINEL_HOST": …, …})`.
  The seven forced keys keep their assignment semantics; without `force=` a dev machine with
  `REDIS_HOST` exported would hang `.delay()`. Then
  `test_overrides(base, cache_location="cafm-backend-test-cache", session_backend="cache",
  celery_eager=True, quiet_db_logging=True, envoy=EnvoySpec(mode="explicit",
  identity=TEST_ENVOY_IDENTITY))`. The ArrayField / `quote_value` patches stay local.
- **redirect-service** — `apply_test_env(SESSION_CUSTOMER_FILTER="TRUE", …)`; the baseline value
  is `"FALSE"` and its suite exercises real org scoping. Its `tests/settings.py` also keeps
  `SESSION_CUSTOMER_FILTER = True` as a Python setting, which `test_overrides()` never emits, so
  nothing overwrites it. The fake-`envoy_pyauth` `sys.modules` install stays local.

### Everyone else

- **backend-template / gateway-service** — `apply_test_env()` +
  `test_overrides(static_root=True)`; the two files become identical by construction. Their
  `SESSION_CUSTOMER_FILTER = False` line is now redundant (the baseline already exports
  `"FALSE"`, which is what `app/settings.py` reads).
- **update-service / stormbreaker** — set `NETIX_TEST_ENVOY_HEADER_GUARD = False` in the test
  settings module and delete `app/test_auth_middleware.py`; drop the `EnvoySpec(middleware=…)`
  argument at the same time.
- **tag-service / data-service** — both declare a `readonly` alias. Neither declares a
  `DATABASE_ROUTERS`, so the default `clear_routers=True` is a no-op for them; a repo that *does*
  test through its router must pass `clear_routers=False`. tag-service should also fold its
  second, larger `REQUIRED_ENV_DEFAULTS` block in `tag/tests/conftest.py` into the single
  `apply_test_env()` call — the two blocks disagree today on `DJANGO_SECRET` and
  `REDIS_PASSWORD`.
- **stormbreaker / visualization-backend** — `clear_routers=True` (the default) reproduces their
  explicit `DATABASE_ROUTERS = []`.
- **asset-service** — `load_base_settings(globals(), skip_existing=True)` +
  `engine="django.contrib.gis.db.backends.spatialite"`; `settings_test_db.py` differs only in
  `engine=` / `database=`.
- **visualization-backend** — move `ENV_DEFAULTS` out of `tests/conftest.py::pytest_configure`
  into `apply_test_env()` in the settings module, ending the split brain.
- **static-service** — its 13-key env block is duplicated verbatim in the repo-root
  `conftest.py`; one `apply_test_env()` call replaces both.
- **ml-engine / report-engine / license-server** — standalone settings, so no
  `load_base_settings`; they still take `test_overrides(None, …)` for DB/cache/session/static
  and `EnvoySpec` for the identity.
- **simulator-service / vision-ai-service** — currently have no env block at all and depend on
  CI-supplied env; adding `apply_test_env()` is net-neutral on line count and removes that
  dependency.

## Deliberately out of scope

- The six mutually incompatible SQLite `ArrayField` shims (user-management, cafm-backend,
  asset-service, data-service, ml-engine, license-server). They sit on migration and
  schema-editor paths and need their own behaviour matrix; a future
  `netix_backend.django.testing.sqlite_arrays` is a real candidate.
- `respx` patterns (zero occurrences in any consumer repo), frozen-time helpers (ml-engine
  only), and the two Redis fakes (two repos, barely overlapping command surfaces).

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
