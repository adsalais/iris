# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
It is a navigator — deep details live in `docs/`.

## Project state

Python web app scaffolded with `uv` / hatchling: **FastAPI + Jinja2** server, **Datastar** (https://data-star.dev/) on the frontend. `src/iris/__init__.py:main()` boots a uvicorn dev server. The home page demonstrates two end-to-end Datastar patterns (see "Examples" below). Pytest is wired up; ruff is configured for lint; basedpyright for typecheck (see Lint & type-check section below).

`requires-python` is currently `>=3.13` — bumped down from 3.14 because the only 3.14 build `uv` could fetch was `3.14.0a6`, on which `pydantic-core` (a FastAPI dep) segfaults. Re-evaluate when a stable 3.14 build is reachable AND pydantic publishes 3.14 wheels.

## Commands

The project uses a `src/`-layout with hatchling as the build backend and `.python-version` pinning 3.13.

- Run the dev server: `uv run iris` (binds 127.0.0.1:8000) or `uv run uvicorn iris.app:build_app --factory --reload` for hot-reload.
- Install/sync after editing `pyproject.toml`: `uv sync`
- Add a runtime dep: `uv add <pkg>` — and `uv add --dev <pkg>` for dev-only.

### Lint & type-check

- `uv run ruff check` — must produce zero warnings.
- `uv run basedpyright --level error` — gate. Must stay at zero errors.
- `uv run basedpyright --level warning` — also at zero. The `[tool.basedpyright]` config in `pyproject.toml` disables a handful of noisy categories that fire on intentional FastAPI/pytest patterns (`reportUnusedCallResult`, `reportUnusedFunction`, `reportCallInDefaultInitializer`, `reportAny`, `reportExplicitAny`, `reportUnannotatedClassAttribute`). The `tests/` execution environment additionally relaxes the unknown-type checks (pytest fixtures and `TestClient` response objects are dynamically typed). `providers/ldap.py` carries file-level pyright suppressions for the same reason — ldap3 is inherently dynamic. New checks failing means a real issue worth investigating, not config drift.
- **Don't use Python's implicit string concatenation.** `reportImplicitStringConcatenation` is on and gates merges. Adjacent string literals on consecutive lines (`f"foo " f"bar"` or `"foo " "bar"`) — including the common pattern of wrapping a long error message across lines — fail the warning gate. Pick one of:
  - **Single string** with the line break in the source: `f"foo bar baz"` (or wrap a long literal in `(...)` parens with no operator).
  - **Explicit `+`** between fragments: `f"foo {x} " + f"bar {y}"`.
  - **Hoist to a variable**: `msg = f"…"; raise TypeError(msg)`.

  Always run `uv run basedpyright --level warning` before committing — `--level error` alone misses this rule.

### implementation

- ALWAYS use  2. *Inline Execution*  Execute tasks in this session using executing-plans, batch execution with checkpoints instead of  1. Subagent-Driven  dispatch a fresh subagent per task, review between tasks, fast iteration
- ALWAYS Create a feature branch

### Tests

Pytest is the test runner. Config lives under `[tool.pytest.ini_options]` in `pyproject.toml` (`testpaths = ["tests"]`, `--import-mode=importlib`).

- Run the full suite: `uv run pytest`
- Run a single file: `uv run pytest tests/test_app.py`
- Run a single test by node id: `uv run pytest tests/test_app.py::test_index_renders`
- Filter by name: `uv run pytest -k <substring>`
- Stop at first failure with verbose tracebacks: `uv run pytest -x -vv`
- Skip both Docker-backed integration suites during dev (Keycloak + ClickHouse boot):
  ```
  uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration
  ```
  The auth-integration suite drives Keycloak; the clickhouse-integration suite chains Keycloak + ClickHouse for end-to-end role/policy testing.


Conventions for new tests:
- Tests live under `tests/` at the repo root (sibling to `src/`), not inside the package.
- **Do not add `__init__.py` under `tests/`** — `--import-mode=importlib` requires `tests/` to *not* be a package, but in exchange every test file must have a unique basename across the suite.
- Import the package as `from iris.app import build_app` (or `from iris import …`). FastAPI's `TestClient(app)` is the standard fixture; use `from fastapi.testclient import TestClient`.

## Conventions


Patterns an agent must follow that aren't obvious from reading code:

- **DDL safety**: external strings flow through `validate_identifier` + `quote_identifier` (`iris.clickhouse.identifiers`). For `kind` in `{database, username, group}`, `validate_identifier` also rejects names ending in iris's reserved role suffixes (`_USER`, `_GRP`, `_DBADMIN`, `_DBWRITER`, `_DBREADER`). String literals embedded in DDL use `quote_sql_literal` (inline literals) or `quote_sql_array_element` (CH array literal elements) — these have different escape grammars and the helper name picks the right one. DML uses CH's `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.
- **Pre-create-on-grant**: tier-grant helpers issue `CREATE ROLE IF NOT EXISTS <target>_USER` before granting. Required for username-enumeration defence; don't shortcut.
- **Session `data` is a per-request snapshot**: mutations don't auto-persist. Routes that want to write through call `await request.app.state.auth_session_store.update_data(session.id, session.data)`.
- **Session methods import directly from `iris.clickhouse.{audit,grants,policies,users,queries}` and call `asyncio.to_thread(<sync_fn>, ...)` inline**: the previous `iris.clickhouse.handle.*_impl` thunk layer was deleted; methods talk to the sync helpers (and `query_as_user` / `query_as_service` for the async-only paths) directly. Don't reintroduce the indirection.
- **One parameter per route**: `session: SessionRead` / `SessionDatabaseAdmin` / etc. carry both admission and capability. Don't pair an alias with a separate handle dep — the handle classes are gone.
- **Refactor pattern**: spec → plan → atomic commit. Big renames go through a deliberate breakage window with one big-bang commit at the end. Don't try to incrementally split refactors that need to be atomic.
- **Tests don't mock the database**: `tests/clickhouse/` uses a real CH testcontainer (session-scoped). Per-test isolation is the `prefix` fixture (UUID-prefixed entity names).

### Operator follow-ups

These are NOT done by iris — call them out for operators wiring up new features:

- **Dict-keyed row policies (`add_row_dict_policy`)** require, BEFORE the policy is useful:
  1. The dict source table exists (any database; arbitrary schema as long as it has the key column and an `Array(String)` attribute column).
  2. The dictionary exists (`CREATE DICTIONARY ...`) with a layout (`COMPLEX_KEY_HASHED` for `String` keys) and a `LIFETIME` matching how often the underlying data changes.
  3. `GRANT dictGet ON <dictionary> TO <role>` for every role the policy is attached to. Without this grant, the per-row evaluation raises `Code: 497` server-side and the user sees zero rows from the policy's perspective (CH treats it as "policy did not match", not a hard error).
- **Open: surface missing-`dictGet` grants in the admin UI.** When the Authorization feature gains awareness of dict policies, the per-database admin view should warn when a role with a dict policy on a table lacks `dictGet` on the referenced dict. Until then, the operator runs `SELECT * FROM system.grants WHERE access_type = 'dictGet'` to verify.

## Architecture & Datastar integration

### Layout

- `src/iris/__init__.py` — calls `load_dotenv()` and defines `main()` (uvicorn factory-mode launcher for the `iris` script).
- `src/iris/app.py` — `build_app()`, Datastar routes, `/`, `/api/greet`, `/api/clock`, and `Jinja2Templates` initialization.
- `src/iris/middleware.py` — `SecurityHeadersMiddleware` (CSP).
- `src/iris/templates/` — Jinja2 templates packaged with the wheel; `base.html` includes the Datastar CDN script and shared CSS, `index.html` extends it.
- `src/iris/auth/` — session-based auth + tier-based authz subsystem. Full surface in `docs/auth.md`.
- `src/iris/clickhouse/` — ClickHouse provisioning + bridge. Full surface in `docs/clickhouse.md`.

### How Datastar talks to the backend

Datastar is hypermedia-first with reactive *signals*. Two flavors of interaction in this repo:

1. **Pure-client reactivity.** A section declares signals via `data-signals="{count: 0}"` and references them with `$count` inside `data-on:click`, `data-text`, `data-show`, etc. No round-trip; the browser handles it.
2. **Server-driven via SSE.** A `data-on:click="@get('/api/greet')"` triggers a fetch. Datastar attaches a `Datastar-Request: true` header and serializes signals into a `datastar` query param (for GET/DELETE) or JSON body (for POST/PUT/PATCH). The server consumes them via the `Signals` annotated dep (see below) and returns a `text/event-stream` response carrying `datastar-patch-elements` events that morph into the DOM by element id.

#### The `Signals` dependency

The SDK's `read_signals(request)` returns `dict | None` (None when the `Datastar-Request` header is absent or the payload is empty). To avoid `or {}` boilerplate in every route, `app.py` defines a thin wrapper and a reusable annotated alias:

```python
async def _signals(request: Request) -> dict[str, Any]:
    return await read_signals(request) or {}

Signals = Annotated[dict[str, Any], Depends(_signals)]
```

Routes then take `signals: Signals` and get a guaranteed dict — no None handling. Use this for any new signal-consuming route. The SDK also ships its own `ReadSignals` annotated alias, but it preserves the `dict | None` type, which is why we shadow it with our own.

### SDK gotchas (already worked around in `app.py`)

- Imports that compose correctly: `from datastar_py.fastapi import DatastarResponse, read_signals, ServerSentEventGenerator as SSE`. Construct responses as `return DatastarResponse(SSE.patch_elements("<div id='x'>...</div>"))`.
- **Avoid `@datastar_response` on FastAPI routes.** FastAPI 0.136's generator-detection mis-classifies the wrapper and routes it through the JSONL streamer, raising `'async for' requires an object with __aiter__ method, got coroutine`. Returning `DatastarResponse(...)` directly sidesteps this.
- Consume signals via the project's `Signals` annotated dep, not by calling `read_signals` inline (see "The `Signals` dependency" above for the why).
- When testing the SSE endpoint, requests must include `headers={"Datastar-Request": "true"}` and pass signals as `params={"datastar": json.dumps({...})}` for GET/DELETE — otherwise `read_signals` returns `None` and `Signals` resolves to `{}` (defaults kick in).
- Always HTML-escape any signal value before interpolating it into a `patch_elements` payload (use `html.escape`); Datastar inserts the bytes as-is.

### Live frontend example

The Authorization feature (`src/iris/features/authorization/`) is the
canonical reference for feature modules. It exercises every defense-in-depth
layer (nav filter / intent gate / per-route `Session*` guard), the tab
system, capability-adaptive rendering, sub-tabs (admin_console), and the
inline-error pattern (create_database). New features should mirror its
shape. Full surface in `docs/frontend.md`.

### Datastar attribute cheatsheet (referenced from data-star.dev)

- `data-signals="{...}"` declares signals; reference them with `$name` in expressions.
- `data-bind="name"` two-way binds a form element to a signal.
- `data-text="$expr"`, `data-show="$expr"`, `data-class="{cls: $expr}"`, `data-attr:foo="$expr"`.
- `data-on:click="..."` (note the colon, not hyphen). Inside the expression, server actions are `@get('/url')`, `@post('/url')`, `@put`, `@delete`, `@patch`.
- Server SSE events: `datastar-patch-elements` (HTML morph by id; `data: selector`, `data: mode`, `data: elements`) and `datastar-patch-signals` (`data: signals <JSON>`). The SDK's `SSE.patch_elements()` / `SSE.patch_signals()` formats these correctly.

## Frontend architecture

Iris's user-facing frontend is a two-panel shell (`src/iris/shell/`) that
hosts feature modules under `src/iris/features/<name>/`. Full surface in
`docs/frontend.md`.

Conventions an agent must follow that aren't obvious from reading code:

- **One feature = one directory** under `src/iris/features/<name>/`. Required
  contents: `install.py` (with public `install(app)` re-exported from
  `__init__.py`), `routes.py` (with `APIRouter(prefix="/feature/<name>")`),
  `intents.py` (with `RENDER_BY_INTENT` mapping intent names to render
  functions), `service.py` (read-side helpers, no FastAPI imports), and
  `templates/<name>/` for Jinja templates. Optional: `static/`.
- **Install order is fixed**: `build_app` calls auth → clickhouse → shell →
  features → `init_templates()`. Features assume `app.state.contributions`
  and `app.state.intent_dispatcher` exist; the shell creates them.
- **Templates**: each subsystem / feature owns its templates dir, registered
  via `iris.templates.register_template_dir(...)` from its `install`. The
  process-wide loader is built once by `init_templates()` after all installs.
  First-registered wins on path collisions; namespace by directory
  (`shell/shell.html`, `auth/forbidden.html`, `authorization/my_access.html`).
- **Tabs are server-side state.** Open tabs live in `session.data['tabs']`
  (a list of `{id, feature, intent, params, title}` dicts). Mutations go
  through `iris.shell.tabs.{append,remove,replace}_tab` then
  `await session.persist_data()`. Refresh restores from `session.data` —
  no localStorage.
- **Per-tab signals** live under `$tabs.<tab_id>.*`. DOM ids inside a tab
  fragment are derived from the tab id via `iris.shell.element_id.el(...)`.
  Server-side only; never compute ids in JS.
- **Datastar discipline.** Server is the source of truth for state. Signals
  carry only ephemeral UI state (`$active`, `$nav_collapsed`, form input
  bindings). All structural changes are SSE patches via
  `DatastarResponse([SSE.patch_elements(...), SSE.patch_signals(...)])`.
  `mode=` arguments use the `ElementPatchMode` enum from `datastar_py.consts`.
  No JS in templates. Lazy-load fragments with `data-init="@get(...)"`
  (NOT `data-on:load` — `load` doesn't fire on `<div>`).
- **Defense in depth, three layers**:
  1. Nav rendering (`render_nav` filters by `Capabilities`).
  2. Intent gate (`POST /api/tabs` runs the intent's `required` predicate).
  3. Per-route guard (every feature route uses `Annotated` `Session*` deps).
  Only (3) enforces; (1) and (2) are UX. Always implement all three.
- **Contribution registry discipline rule.** Do not add a new registry to
  `iris.shell.contributions.Contributions` until at least one feature has a
  concrete need to contribute and at least one feature has a concrete need
  to consume. Every registry is permanent API surface.
- **No cross-feature imports.** Features may import `iris.auth`,
  `iris.clickhouse`, `iris.shell` — never another feature. Cross-feature
  integration goes through the contribution registry. (Soft rule for now;
  reconsider if a real exception appears.)
- **CSRF on every state-changer.** Datastar `@post` / `@put` / `@patch` /
  `@delete` routes use `Depends(verify_csrf_header)`. Form POSTs use
  `verify_csrf_form`.
- **Tab cap.** `MAX_TABS_PER_SESSION = 32`. Over the cap returns 409.

## Module map

```
src/iris/
├── __init__.py        # main() + load_dotenv
├── app.py             # build_app(): wires auth, ch, shell, features
├── middleware.py      # SecurityHeadersMiddleware (CSP)
├── templates.py       # register_template_dir / init_templates registry
├── auth/              # auth subsystem — full surface in docs/auth.md
├── clickhouse/        # CH subsystem — full surface in docs/clickhouse.md
├── shell/             # frontend shell — full surface in docs/frontend.md
├── features/          # feature modules — one dir per feature
│   └── authorization/  # Authorization (my_access / manage / create_database / admin_console)
└── static/            # global vendored assets (datastar.js)
```

## Env vars (quick reference)

| Var | Purpose |
|---|---|
| `AUTH_METHOD` | `oauth` / `ldap` / `mock` |
| `SESSION_*` | session TTLs, cookie name, max-per-user |
| `AUTH_DB_PATH` | SQLite session store path; `:memory:` for tests |
| `COOKIE_SECURE` | set `false` for local dev over http |
| `OIDC_*` | OAuth/OIDC discovery (when `AUTH_METHOD=oauth`) |
| `LDAP_*` | LDAP bind + group search (when `AUTH_METHOD=ldap`) |
| `MOCK_*` | mock provider (when `AUTH_METHOD=mock`) |
| `CLICKHOUSE_HOST` / `_PORT` / `_USER` / `_PASSWORD` | CH connection |
| `CLICKHOUSE_SECURE` / `_VERIFY` / `_CA_CERT_PATH` | TLS settings |
| `CLICKHOUSE_ADMIN_USER` | bootstrap admin's IdP username |
| `CLICKHOUSE_ADMIN_GROUP` | bootstrap admin group's IdP name |

Full descriptions, `.env` semantics, and operator runbooks live in `docs/operations.md`.

## See also

- `docs/auth.md` — full auth surface (alias deps, Session hierarchy, providers, login flows, tests)
- `docs/clickhouse.md` — full CH surface (tier roles, bootstrap, row policies, the bridge with auth)
- `docs/frontend.md` — full frontend surface (shell, contributions, tabs, Datastar conventions)
- `docs/operations.md` — deployment, env-var depth, security follow-ups, migration runbooks
