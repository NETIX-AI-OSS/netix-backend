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
