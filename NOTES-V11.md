# v1.1.0 notes — `netix_backend.asgi` (merge into CHANGELOG.md / README.md, then delete)

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
