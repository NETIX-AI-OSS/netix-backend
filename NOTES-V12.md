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
