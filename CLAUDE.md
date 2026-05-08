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

### Tests

Pytest is the test runner. Config lives under `[tool.pytest.ini_options]` in `pyproject.toml` (`testpaths = ["tests"]`, `--import-mode=importlib`).

- Run the full suite: `uv run pytest`
- Run a single file: `uv run pytest tests/test_app.py`
- Run a single test by node id: `uv run pytest tests/test_app.py::test_index_renders`
- Filter by name: `uv run pytest -k <substring>`
- Stop at first failure with verbose tracebacks: `uv run pytest -x -vv`

Conventions for new tests:
- Tests live under `tests/` at the repo root (sibling to `src/`), not inside the package.
- **Do not add `__init__.py` under `tests/`** — `--import-mode=importlib` requires `tests/` to *not* be a package, but in exchange every test file must have a unique basename across the suite.
- Import the package as `from iris.app import build_app` (or `from iris import …`). FastAPI's `TestClient(app)` is the standard fixture; use `from fastapi.testclient import TestClient`.

## Conventions

Patterns an agent must follow that aren't obvious from reading code:

- **DDL safety**: external strings flow through `validate_identifier` + `quote_identifier` (`iris.clickhouse.identifiers`). Never f-string-concat raw user input into SQL. DML uses CH's `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.
- **Pre-create-on-grant**: tier-grant helpers issue `CREATE ROLE IF NOT EXISTS <target>_USER` before granting. Required for username-enumeration defence; don't shortcut.
- **Session `data` is a per-request snapshot**: mutations don't auto-persist. Routes that want to write through call `await request.app.state.auth_session_store.update_data(session.id, session.data)`.
- **Session methods use top-level imports of `iris.clickhouse.handle.*_impl`**: lazy method-body imports were a workaround for a now-removed cycle. Don't regress.
- **One parameter per route**: `session: SessionRead` / `SessionDatabaseAdmin` / etc. carry both admission and capability. Don't pair an alias with a separate handle dep — the handle classes are gone.
- **Refactor pattern**: spec → plan → atomic commit. Big renames go through a deliberate breakage window with one big-bang commit at the end. Don't try to incrementally split refactors that need to be atomic.
- **Tests don't mock the database**: `tests/clickhouse/` uses a real CH testcontainer (session-scoped). Per-test isolation is the `prefix` fixture (UUID-prefixed entity names).

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

### Examples currently in `index.html`

- **Counter** — `data-signals="{count: 0}"`, `data-on:click="$count++"`, `data-text="$count"`. Pure client.
- **Greeting** — `<input data-bind="name">` two-way-bound to a `name` signal; the button calls `@get('/api/greet')`; the server returns an `id="greeting"` fragment that morphs into the placeholder `<div id="greeting">`.
- **Server clock** — long-lived SSE stream demonstrating `async def` generators. The `_clock_stream` generator `yield`s a `SSE.patch_signals({"now": ...})` event every second; `clock()` wraps `_clock_stream()` in `DatastarResponse`. One HTTP request, infinite events. Note: TestClient (sync) deadlocks on infinite SSE responses, so the generator is unit-tested directly via `asyncio.run(_clock_stream().__anext__())` rather than through the route — verify the route end-to-end with `curl -N http://127.0.0.1:8000/api/clock`.

### Datastar attribute cheatsheet (referenced from data-star.dev)

- `data-signals="{...}"` declares signals; reference them with `$name` in expressions.
- `data-bind="name"` two-way binds a form element to a signal.
- `data-text="$expr"`, `data-show="$expr"`, `data-class="{cls: $expr}"`, `data-attr:foo="$expr"`.
- `data-on:click="..."` (note the colon, not hyphen). Inside the expression, server actions are `@get('/url')`, `@post('/url')`, `@put`, `@delete`, `@patch`.
- Server SSE events: `datastar-patch-elements` (HTML morph by id; `data: selector`, `data: mode`, `data: elements`) and `datastar-patch-signals` (`data: signals <JSON>`). The SDK's `SSE.patch_elements()` / `SSE.patch_signals()` formats these correctly.

## Module map

```
src/iris/
├── __init__.py        # main() + load_dotenv
├── app.py             # build_app(), Datastar routes, /, /api/greet, /api/clock
├── middleware.py      # SecurityHeadersMiddleware (CSP)
├── templates/         # Jinja2 — base.html + index.html
├── auth/              # auth subsystem — full surface in docs/auth.md
└── clickhouse/        # CH subsystem — full surface in docs/clickhouse.md
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
- `docs/operations.md` — deployment, env-var depth, security follow-ups, migration runbooks
- `docs/superpowers/specs/` — dated design specs (the *why* behind the current shape)
- `docs/superpowers/plans/` — implementation plans (paired with each spec)
