# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

Python web app scaffolded with `uv` / hatchling: **FastAPI + Jinja2** server, **Datastar** (https://data-star.dev/) on the frontend. `src/iris/__init__.py:main()` boots a uvicorn dev server. The home page demonstrates two end-to-end Datastar patterns (see "Examples" below). Pytest is wired up; no linter or formatter yet.

`requires-python` is currently `>=3.13` — bumped down from 3.14 because the only 3.14 build `uv` could fetch was `3.14.0a6`, on which `pydantic-core` (a FastAPI dep) segfaults. Re-evaluate when a stable 3.14 build is reachable AND pydantic publishes 3.14 wheels.

## Commands

The project uses a `src/`-layout with hatchling as the build backend and `.python-version` pinning 3.13.

- Run the dev server: `uv run iris` (binds 127.0.0.1:8000) or `uv run uvicorn iris.app:app --reload` for hot-reload.
- Install/sync after editing `pyproject.toml`: `uv sync`
- Add a runtime dep: `uv add <pkg>` — and `uv add --dev <pkg>` for dev-only.

### Lint & type-check

- `uv run ruff check` — currently produces one intentional `E402` in `src/iris/__init__.py` (the `from iris.app import app` must follow `load_dotenv()` so `.env` populates env first).
- `uv run basedpyright --level error` — gate. Must stay at zero errors.
- `uv run basedpyright --level warning` — also at zero. The `[tool.basedpyright]` config in `pyproject.toml` disables a handful of noisy categories that fire on intentional FastAPI/pytest patterns (`reportUnusedCallResult`, `reportUnusedFunction`, `reportCallInDefaultInitializer`, `reportAny`, `reportExplicitAny`, `reportUnannotatedClassAttribute`). The `tests/` execution environment additionally relaxes the unknown-type checks (pytest fixtures and `TestClient` response objects are dynamically typed). `mapping.py` and `providers/ldap.py` carry file-level pyright suppressions for the same reason — yaml and ldap3 are inherently dynamic. New checks failing means a real issue worth investigating, not config drift.

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
- Import the package as `from iris.app import app` (or `from iris import …`). FastAPI's `TestClient(app)` is the standard fixture; use `from fastapi.testclient import TestClient`.

## Architecture & Datastar integration

### Layout

- `src/iris/__init__.py` — re-exports `app` and defines `main()` (uvicorn launcher for the `iris` script).
- `src/iris/app.py` — FastAPI app, routes, and `Jinja2Templates` initialization.
- `src/iris/templates/` — Jinja2 templates packaged with the wheel; `base.html` includes the Datastar CDN script and shared CSS, `index.html` extends it.
- `tests/test_app.py` — route-level tests via FastAPI's `TestClient`.

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

## Authentication

The `iris.auth` package adds session-based authentication to all routes. Public surface:

```python
from iris.auth import Session, OptionalSession, require_role, User, install
```

`Session` requires a valid session (only via the `iris_session` cookie); routes that take it 401 if no session is present. `OptionalSession` returns `None` when there's no session and never raises. Both are FastAPI dependency aliases — use them as parameter type annotations (`async def f(session: Session): ...`) and the dep system fills in a request-scoped `Session` view.

The `Session` view exposes everything routes legitimately need from a logged-in session: `id`, `user` (a `User`), `created_at`, `expires_at`, `data` (the per-session mutable dict), and `roles` (a `frozenset[str]` of effective role names with `includes:` closure already applied). The `data` field is the same dict object as the session store's storage, so `session.data[key] = value` writes through with no commit step. All other fields are frozen.

`require_role("admin")` is a dependency factory that 403s if the user's effective role set doesn't contain the named role. It returns a `Session`, so role-gated routes write `session: Session = Depends(require_role("admin"))` and access `session.user`/`session.data`/`session.roles` from the same value. See "Authorization (roles)" below for the schema and inheritance semantics.

**Two `Session` names, two import paths.** FastAPI 0.136 raises `AssertionError: Cannot specify Depends in Annotated and default value together` when an Annotated alias with `Depends` is combined with `= Depends(other)`. So:

- **Bare-auth routes** (no role check) write `session: Session` with no `=`. Import the **alias** from the package: `from iris.auth import Session`. The alias has `Depends(_build_required)` baked into its `Annotated` metadata, which FastAPI uses to inject the value.
- **Role-gated routes** write `session: Session = Depends(require_role("admin"))`. Import the **class** from the submodule: `from iris.auth.session import Session`. The class has no `Depends` metadata, so the explicit `= Depends(require_role(...))` provides the dep.
- Both `Session` names evaluate to the same underlying class for type-checker purposes (`Annotated[X, ...]` IS `X` for typing), so `session.user`, `session.data`, etc. work identically in either form.
- **A file mixing both patterns** imports the alias under a local name. Convention: `from iris.auth import Session as RequireSession` (used by `tests/auth/authz/test_authz_deps.py`).
- If you ever see FastAPI raise the "Cannot specify Depends in Annotated and default value together" error at app construction, you imported the wrong `Session` for the route style. Switch the import path.

### Per-session server-side data

Each `UserSession` carries a mutable `data: dict[str, Any]` field for arbitrary route-managed state (drafts, wizard steps, recently-viewed lists, etc.). The `Session` dep exposes it via `session.data`:

```python
from fastapi import Request
from iris.auth import Session

@app.post("/draft")
async def save_draft(request: Request, session: Session, body: dict):
    session.data["draft"] = body
    await request.app.state.auth_session_store.update_data(
        session.id, session.data
    )
    return {"ok": True}

@app.get("/draft")
async def get_draft(session: Session):
    return session.data.get("draft", {})

@app.get("/me/full")
async def me_full(session: Session):
    return {
        "id": session.id,
        "logged_in_at": session.created_at,
        "data_keys": list(session.data),
        "roles": sorted(session.roles),
    }
```

- `session.data` is a per-request snapshot — a fresh `dict` deserialized from the SQLite row on every request. Mutations to the dict do **not** auto-persist; routes that want the change to survive call `await request.app.state.auth_session_store.update_data(session.id, session.data)` before returning.
- `Session` exposes `id`, `user`, `created_at`, `expires_at`, `data`, and `roles` on a single value. Routes that need only the user write `session.user`; routes that need the per-session bag write `session.data`.

A single `Session` dep injection resolves the underlying session lookup, the per-request `Session` view construction, and the role computation exactly once. A route taking both `session: Session` and `Depends(require_role(...))` makes one store hit, computes roles once, and runs the role check on the cached view.

Lifecycle: `data` is JSON-encoded into the SQLite row alongside the session. Mutations are persisted by `update_data` and survive process restarts. Values must be JSON-encodable (strings, ints, floats, bools, `None`, lists, dicts) — anything else raises `TypeError` at write time. Read-modify-write across an `await` between two requests for the same session has the standard interleaving race; acceptable at ≤20-user scale, document or use `asyncio.Lock` if a route needs atomic updates.

### Authorization (roles)

Application code references **internal role names only** (`admin`, `writer`, `reader`, etc.). The mapping from role → external IdP groups/usernames lives in SQLite, in the same `AUTH_DB_PATH` file as the session store. Routes never reference IdP group names directly; they use `Depends(require_role("admin"))`. Operators edit the mapping via the `RoleMappingStore` API (future admin routes); no file edits, no app restart.

**Schema** (four tables, `authz_*` prefix):

```sql
CREATE TABLE authz_roles (
    name TEXT PRIMARY KEY                     -- regex: [a-zA-Z0-9_-]+
);
CREATE TABLE authz_role_groups (
    role_name  TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (role_name, group_name),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);
CREATE TABLE authz_role_users (
    role_name      TEXT NOT NULL,
    username_lower TEXT NOT NULL,             -- case-insensitive: stored lowercased
    PRIMARY KEY (role_name, username_lower),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);
CREATE TABLE authz_role_includes (
    role_name     TEXT NOT NULL,
    included_role TEXT NOT NULL,
    PRIMARY KEY (role_name, included_role),
    FOREIGN KEY (role_name)     REFERENCES authz_roles(name) ON DELETE CASCADE,
    FOREIGN KEY (included_role) REFERENCES authz_roles(name) ON DELETE RESTRICT
);
```

`ON DELETE CASCADE` on the child tables means dropping a role removes its assignments automatically. `ON DELETE RESTRICT` on `included_role` prevents deleting a role that another role still includes. Cycles are rejected app-side on `add_include` — SQLite can't enforce graph acyclicity.

**Mutator API** — two paths, same underlying SQL:

1. **Session-scoped (recommended for routes).** Obtain a mutator from the store; each call re-checks the session has the configured admin role:

```python
mutator = request.app.state.authz_store.for_session(session)
await mutator.add_role(name)
await mutator.remove_role(name)
# ... etc — same surface as the bare store ...
```

`for_session(session, *, required_role="admin")` returns a `RoleMappingStoreMutator` bound to the caller's session. Defense-in-depth: even if a route forgets the `Depends(require_role("admin"))` gate, the mutator catches the call with `AuthForbidden`. Operators with a non-default authz-admin role pass it explicitly.

2. **Bare store (internal trusted code only).** Bootstrap, install, and test fixtures call directly:

```python
await store.add_role(name)
await store.remove_role(name)             # raises if another role includes it
await store.add_group_to_role(role, group)
await store.remove_group_from_role(role, group)
await store.add_user_to_role(role, username)         # username lowercased on storage
await store.remove_user_from_role(role, username)
await store.add_include(role, included_role)        # cycle-checked app-side
await store.remove_include(role, included_role)
```

Routes should NOT call the bare store; route bugs that bypass `for_session` lose the defense-in-depth check.

Each mutator validates inputs (role names against `[a-zA-Z0-9_-]+`) and translates SQLite FK violations into `RoleMappingError` with a clean message. `add_*` are idempotent (`INSERT OR IGNORE`).

**Identity matching:**
- `groups` — exact, case-sensitive match against `User.groups` (verbatim from the IdP).
- `users` — case-insensitive match against `User.username`.
  - OAuth provider sources `username` from the `preferred_username` claim, falling back to `sub` if absent. If your OIDC IdP doesn't issue `preferred_username`, your `users` lists must contain the `sub` UUIDs.
  - LDAP provider sources `username` from the `username` substituted into `LDAP_BIND_DN_TEMPLATE`.
  - Mock provider sources `username` from `MOCK_USERNAME`.

**Use in routes:**

```python
from iris.auth.session import Session
from iris.auth.authz.deps import require_role

@app.get("/docs")
async def list_docs(session: Session = Depends(require_role("reader"))):
    ...
```

For routes that want bare auth and need to read roles:

```python
from iris.auth import Session

@app.get("/me/roles")
async def my_roles(session: Session):
    return {"roles": sorted(session.roles)}
```

These two examples illustrate the dual-import pattern: the **class** (`from iris.auth.session import Session`) for role-gated routes that combine `= Depends(require_role(...))` with the type, and the **alias** (`from iris.auth import Session`) for bare-auth routes that rely on the alias's baked-in `Depends` metadata.

`require_role("reader")` admits any user whose effective role set contains `reader`, directly or via `includes` (so admins and writers get in too). `session.roles` returns the user's full effective role set as a `frozenset[str]` — useful for templates and `/api/whoami`-style endpoints.

If a route names a role that isn't defined in the DB, the request returns **500** (not 403) with a generic body — silent 403s would mask operator typos like `require_role("reder")`. The missing role name is logged server-side.

**First-install bootstrap.** Two env vars seed the initial admin user:

```
AUTHZ_BOOTSTRAP_ROLE=admin       # default: "admin"
AUTHZ_BOOTSTRAP_USER=alice       # if unset, no bootstrap
```

`install_authz_schema` runs at app boot. If `authz_roles` doesn't yet exist, it creates the schema AND seeds:
- a row in `authz_roles` for the bootstrap role (default `admin`),
- a row in `authz_roles` for `clickhouse_admin`,
- an include edge `(admin → clickhouse_admin)` so the seeded user immediately gets ClickHouse admin powers,
- a row in `authz_role_users` adding the bootstrap user to the admin role.

Once tables exist, the function only ensures the schema (idempotent) and leaves content alone. Operators can rename/delete the bootstrap role, change includes, remove the bootstrap user — restart won't fight them. Wiping the DB file re-triggers bootstrap.

If `AUTHZ_BOOTSTRAP_USER` is unset on a fresh DB, the tables are empty. Role-gated routes 500 until the operator populates the mapping via `app.state.authz_store` calls.

The hardcoded string `"clickhouse_admin"` in `iris.auth.authz.bootstrap` must match `iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE`. A drift test in `tests/auth/authz/test_authz_bootstrap.py` asserts equality.

### Configuration

Env vars are loaded at `import iris` time via `python-dotenv`. If a `.env` file exists at the project root (gitignored), its values populate `os.environ` for any keys not already set. If `.env` is absent it's a silent no-op. **Real shell env vars take precedence over `.env`** (`load_dotenv` is called with `override=False`), so a CI / production deploy can override individual values without editing `.env`. Tests inherit the same loader; `tests/conftest.py` sets `os.environ.setdefault(...)` defaults at module scope before iris is imported, so test runs always end up with `AUTH_METHOD=mock` regardless of what `.env` contains — this protects the test suite from a developer's OAuth/LDAP `.env`.

Selected by env var. Per-deployment toggle: only one method is active at a time.

```
AUTH_METHOD=oauth | ldap | mock
SESSION_COOKIE_NAME=iris_session
SESSION_TTL_SECONDS=43200            # 12h, sliding TTL refreshed on each request
SESSION_ABSOLUTE_TTL_SECONDS=2592000 # 30d, hard cap on top of sliding TTL
SESSION_MAX_PER_USER=10              # cap concurrent sessions per User.subject (oldest evicted)
AUTH_DB_PATH=./iris-auth.db          # SQLite file backing sessions, authz, and per-database admin tables; :memory: for tests
COOKIE_SECURE=true                   # set false for local dev over http
AUTHZ_BOOTSTRAP_ROLE=admin           # role created on first install (when authz_roles doesn't yet exist)
AUTHZ_BOOTSTRAP_USER=                # if set, seeded into the bootstrap role on first install

# OAuth (OIDC discovery)
OIDC_ISSUER_URL=https://keycloak.example.com/realms/iris
OIDC_CLIENT_ID=iris
OIDC_CLIENT_SECRET=...
OIDC_SCOPES=openid profile email groups
OIDC_CA_CERT_PATH=                     # optional: PEM bundle for IdP cert validation (private CA)

# LDAP
LDAP_URL=ldaps://ldap.example.com:636
LDAP_BIND_DN_TEMPLATE=uid={username},ou=people,dc=corp,dc=local
LDAP_GROUP_BASE_DN=ou=groups,dc=corp,dc=local
LDAP_REQUIRE_TLS=true                # reject ldap:// at startup
LDAP_CA_CERT_PATH=                   # optional: PEM bundle for cert validation

# Mock (for tests; AUTH_METHOD=mock)
MOCK_USERNAME=alice
MOCK_PASSWORD=secret
MOCK_GROUPS=admins,users
MOCK_DISPLAY_NAME=Alice
```

`AuthSettings.from_env()` runs at app construction; missing required vars or unrecognized values fail loudly. `_get_bool` raises on typos (`COOKIE_SECURE=ture` is rejected, not silently false).

**`.env` permissions:** the file may contain secrets (`OIDC_CLIENT_SECRET`, `MOCK_PASSWORD`, etc.). On a multi-user host, `chmod 600 .env` so it's only readable by the iris service user. The file is gitignored; check that your container/build pipeline doesn't bake it into images.

### Multi-worker deployment

Sessions live in a SQLite file; multiple uvicorn workers share state by pointing at the same `SESSION_DB_PATH`. The store opens its connection in WAL mode (`PRAGMA journal_mode=WAL`) so concurrent readers don't block on a writer, and `PRAGMA synchronous=NORMAL` keeps writes cheap. Workers can scale freely on a single host (e.g., `uvicorn --workers 4`) as long as the DB path is on local disk reachable by every worker. Cross-host deploys still need a shared filesystem — or swap the store backend.

Sessions also survive process restarts. `uv run iris` and a redeploy no longer log every user out.

### Module map

```
src/iris/auth/
├── __init__.py        # public surface: Session, OptionalSession, require_role, User, install
├── session.py         # Session frozen dataclass (request-scoped view)
├── config.py          # AuthSettings.from_env() + per-method sub-settings
├── identity.py        # User (frozen+slots), UserSession (mutable for sliding TTL; internal)
├── sessions.py        # SessionStore (SQLite): create / get_and_refresh / update_data / delete / close
├── exceptions.py      # AuthRequired, AuthForbidden, AuthError, AuthorizationMisconfigured + install_exception_handlers
├── deps.py            # Session, OptionalSession, set_session_store, set_settings
├── csrf.py            # double-submit CSRF: mint_csrf_token, attach_csrf_cookie, issue_csrf_token, verify_csrf_form, delete_csrf_cookie
├── rate_limit.py      # TokenBucket: in-process per-key token-bucket limiter (used on POST /login)
├── routes.py          # /login, /login/callback, /logout, /api/whoami; install(app)
├── providers/
│   ├── __init__.py    # build_provider(settings) factory dispatching AUTH_METHOD
│   ├── base.py        # Provider Protocol
│   ├── mock.py        # MockProvider (config-driven creds, returns configured groups)
│   ├── ldap.py        # LDAPProvider (ldap3 bind + group search; tests use MOCK_SYNC)
│   └── oauth.py       # OAuthProvider (OIDC discovery + PKCE + signed-cookie state)
└── authz/
    ├── __init__.py    # (empty package marker)
    ├── mapping.py     # RoleDef / RoleMapping value types + compute_closure helper
    ├── store.py       # RoleMappingStore: get_mapping + 8 mutators + close
    ├── bootstrap.py   # install_authz_schema: first-install schema creation + seeding
    ├── core.py        # resolve_roles, current_mapping helpers (read from app.state.authz_store)
    └── deps.py        # require_role(name) factory
```

`install(app)` reads env, builds the provider, and wires the auth router + exception handlers + session store into a FastAPI app. Called from `build_app()` in `src/iris/app.py`.

### Authorization model

Roles are internal names (`admin`, `writer`, `reader`, …) defined in SQLite (`authz_*` tables in `AUTH_DB_PATH`), mapped to external IdP groups and/or usernames. Routes guard themselves with `Depends(require_role("admin"))` — they never reference IdP group names directly. Operators edit the mapping via `app.state.authz_store` (or future admin routes) to (re)map roles to whatever the deployment's IdP exposes; no code change needed when group names differ across OAuth and LDAP. See "Authorization (roles)" above for the schema, mutator API, and inheritance semantics.

### Login flows

- **OAuth (`AUTH_METHOD=oauth`)** — `/login` 302s to the IdP authorize URL with PKCE S256 + state in a signed cookie. The IdP redirects back to `/login/callback`, which exchanges the code, verifies the returned `id_token` (RS256/ES256 signature against the IdP's JWKS, plus `iss`/`aud`/`exp` claims), fetches userinfo, and creates a session. JWKS is fetched once at app construction; rotating IdP keys requires app restart. `next` is preserved across the round-trip via the same signed cookie.
- **LDAP/Mock (`AUTH_METHOD=ldap`/`mock`)** — `/login` renders an HTML form (Jinja template `templates/auth/ldap_form.html`) with a CSRF token. POST `/login` validates CSRF, calls `provider.authenticate(username, password)`, and creates a session on success. Bad creds redirect back to `/login?error=invalid_credentials&next=...`.
- **Logout** — `POST /logout` (CSRF-required) deletes the session and clears the cookie. Local-only — does not call the IdP's end-session endpoint.

The CSRF cookie is rotated on successful login: the post-auth `/login` redirect (and OAuth callback) clear the `iris_csrf` cookie so any pre-auth token capture becomes useless. The next form render (e.g., `/` re-mints via `attach_csrf_cookie`) issues a fresh token, so the user flow is uninterrupted.

`POST /login` is rate-limited per client IP via an in-process token bucket (capacity 10, refill 0.2/sec — i.e. 10-attempt burst then ~12 attempts/minute sustained). Exhausted clients receive a 429 with `Retry-After`. Per-process state, fits the `--workers 1` deploy constraint; multi-worker would need Redis.

### Tests

`tests/conftest.py` sets `AUTH_METHOD=mock` (and the mock creds) at MODULE scope (via `os.environ.setdefault`) so `iris.app:app` can be imported by the suite without arranging env in a fixture. Available fixtures:

- `client` — unauthenticated `TestClient`. Use for tests that exercise the login flow itself, error pages, etc.
- `authed_client` — pre-creates a session in the in-memory store and attaches the cookie. Use for feature tests of routes that just need "a logged-in user".

Provider tests are offline:
- LDAP: `ldap3.MOCK_SYNC` strategy with an in-memory directory (`tests/auth/test_provider_ldap.py`).
- OAuth: `httpx.MockTransport` mocking discovery / token / userinfo (`tests/auth/test_provider_oauth.py`).

### Integration tests (`tests/auth/integration/`)

A second tier under `tests/auth/integration/` runs the OAuth provider end-to-end against a real `quay.io/keycloak/keycloak:26.0` container via `testcontainers-python`. Covers happy paths and natural failure paths exercisable against a real IdP (wrong client secret, code reuse, redirect_uri mismatch, wrong CA bundle) plus full TLS coverage. The existing offline tests stay as the fast unit tier. (LDAP integration tests were originally in scope but descoped — see `docs/superpowers/plans/2026-05-05-auth-testcontainers.md`.)

- Run only the integration tier: `uv run pytest tests/auth/integration`
- Skip the integration tier (no Docker required): `uv run pytest --ignore=tests/auth/integration`
- Runtime: ~25s on a warm cache (Keycloak boot ~12s dominates). Session-scoped containers amortize across the full integration suite.

The realm seed at `tests/auth/integration/seed/keycloak-realm.json` is committed and declarative — it defines an `iris-test` realm with two users (`alice`/`secret` in `admins`+`users`, `bob`/`hunter2` in `users`) and an `iris` client wired up with an explicit `oidc-group-membership-mapper`. Without that mapper Keycloak doesn't emit a `groups` claim, so users would land in iris with `groups=()`. TLS certs are generated at session start via `_tls.py` and not committed. The `_keycloak_helpers.simulate_login` helper drives Keycloak's authorize → login form → callback flow through `TestClient`; the form-action regex is the only place coupled to Keycloak's login HTML.

`OIDC_SCOPES` for the integration tests is `openid profile email` (no `groups`). The realm doesn't ship a `groups` client scope by default, but the client-level mapper emits the claim regardless of requested scope — so production deployments can choose to add a `groups` scope or rely on the mapper directly.

### Open redirect protection

`_safe_next(url)` accepts only same-origin relative paths. Rejects empty, non-`/`-prefixed, `//`-prefixed (protocol-relative), absolute URLs, and backslash-containing strings (browsers normalize `\` → `/` before same-origin checks). Applied at `POST /login` and `GET /login/callback`. Failure-redirect URLs are constructed via `urllib.parse.urlencode` so error tokens or path components can't break query parsing.

### Open security follow-ups (v1.1)

- Rate limiting on `POST /login` keys on `request.client.host`. Behind a reverse proxy this is the proxy's IP — the bucket becomes effectively global. Mitigation: run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>` so `request.client.host` reflects the `X-Forwarded-For` value. Not enforced; deployment-config concern.
- `OAuthProvider` caches the IdP's JWKS once on first discovery. If the IdP rotates signing keys, all logins fail until iris is restarted. Acceptable at ≤20-user / multi-month rotation cadence; tighten by re-fetching on `kid`-not-in-set if rotation matters.
- OIDC discovery is now lazy: the *first* login attempt after restart pays the discovery latency. Acceptable for v1, but means a slow IdP shifts startup latency to a request boundary instead of failing loud at boot.
- `RoleMappingStore.get_mapping()` runs four SELECTs per request to assemble the closure. Sub-millisecond at ≤20-user scale; for higher request volumes, add an in-process cache with a version-column invalidation.

These are accepted residual risks for the ≤20-user / `--workers 1` deploy profile; revisit when scaling out or relocating behind a load balancer.

## ClickHouse

The `iris.clickhouse` package provisions ClickHouse users, roles, grants, and row policies, provides audit-query helpers, and (via the bridge submodule) hands FastAPI routes typed handles for impersonated/admin queries. The plain-data helpers (`audit.py`, `bootstrap.py`, `client.py`, `grants.py`, `policies.py`, `users.py`) are independent of `iris.auth`; only `deps.py` and `install.py` import from auth.

### Public surface

```python
from iris.clickhouse import (
    # plain-data helpers
    ClickHouseSettings, build_client, ensure_service_admin,
    init_user_rights,
    grant_select_to_database, grant_insert_update_to_table,
    add_row_policy, revoke_row_policy,
    user_grants, role_grants, user_role_memberships,
    user_row_policies, role_row_policies, table_row_policies,
    # FastAPI bridge
    ClickHouseHandle, ClickHouseAdminHandle,
    get_clickhouse_handle, require_clickhouse_admin,
    install, CLICKHOUSE_ADMIN_ROLE,
)
```

`build_client(settings)` returns a `clickhouse_connect.driver.client.Client`. Operations take that client as their first argument:

```python
settings = ClickHouseSettings.from_env()
client = build_client(settings)
ensure_service_admin(client, settings)               # idempotent startup
init_user_rights(client, username="alice", groups=["sales"], settings=settings)
add_row_policy(client, database="orders", table="lines",
               column="region", role="alice_USER", value="EU", settings=settings)
```

### Conventions

- Per-user role: `<username>_USER` (suffix is hardcoded at `users.USER_ROLE_SUFFIX`).
- Per-group role: `<group>_GRP` (suffix is hardcoded at `users.GROUP_ROLE_SUFFIX`).
- Row-policy name: `<database>_<table>_<role>_<slug>_<8charhash>` — slug strips non-`[a-zA-Z0-9_]`, hash disambiguates collisions like `EU/UK` vs `EU UK`.
- Wildcard service-admin policy per table: `<database>_<table>_<service_admin_role>` — `USING 1` applied to the role configured in `CLICKHOUSE_SERVICE_ADMIN_ROLE`. Created by `add_row_policy` if missing; *not* dropped by `revoke_row_policy`.
- All operations are idempotent: re-running is safe. `init_user_rights` reconciles group memberships (revokes `_GRP` roles no longer in the input, grants the new ones).

### DDL safety

`identifiers.py` is the single safety contract. External-source strings (usernames from auth, db/table/column names from callers) flow through `validate_identifier` (rejects anything outside `[a-zA-Z0-9_]+`) and `quote_identifier` (validates + backticks). Row-policy values use `quote_string` for SQL literal escaping. DDL is built from these helpers; `client.command()` runs it without parameter binding. DML (audit `SELECT`s) uses ClickHouse's native `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.

### Configuration

Env vars (loaded at `import` time via `python-dotenv` from `.env`):

```
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=iris_service          # CH login iris connects as
CLICKHOUSE_PASSWORD=replace-me
CLICKHOUSE_SECURE=true                # https
CLICKHOUSE_VERIFY=true                # TLS verification
# CLICKHOUSE_CA_CERT_PATH=/etc/ssl/certs/ca-bundle.crt

CLICKHOUSE_SERVICE_ADMIN_USER=iris_service       # IMPERSONATE grantee, normally = CLICKHOUSE_USER
CLICKHOUSE_SERVICE_ADMIN_ROLE=service_admin_role # wildcard-policy grantee; granted to admin user at startup
```

`ClickHouseSettings.from_env()` validates everything at app construction — missing required vars, typo'd booleans (`COOKIE_SECURE=ture` style), non-int ports, and bad identifier names all fail loudly.

### Auth ↔ ClickHouse bridge

Routes that need to query ClickHouse declare one of two FastAPI deps:

```python
from iris.clickhouse import (
    ClickHouseHandle, ClickHouseAdminHandle,
    get_clickhouse_handle, require_clickhouse_admin,
)

@app.get("/click-user")
async def click_user(
    handle: ClickHouseHandle = Depends(get_clickhouse_handle),
):
    rows = await handle.query_as_user("SELECT count() FROM orders.lines")
    return rows  # list[dict[str, Any]] from JSONEachRow

@app.get("/click-admin")
async def click_admin(
    handle: ClickHouseAdminHandle = Depends(require_clickhouse_admin),
):
    return await handle.user_grants(username="alice")
```

`get_clickhouse_handle` admits any logged-in user; the handle exposes only `query_as_user`. `require_clickhouse_admin` 403s users without the `clickhouse_admin` role (and 500s if the role isn't defined in the authz mapping); on success it returns a `ClickHouseAdminHandle` that adds `query_as_service` (no impersonation), the `grant_*`/`add_row_policy`/`revoke_row_policy` mutators, and the audit helpers (`user_grants`, `role_grants`, `user_row_policies`, …). Admin routes that want a user-impersonated query can still call `handle.query_as_user(...)` on the admin handle.

**Why two HTTP transports.** `query_as_user` prepends `EXECUTE AS <quoted_username>` to the SQL. ClickHouse's `EXECUTE AS user <SELECT>` body grammar rejects `FORMAT` clauses, but `clickhouse-connect`'s `query()` always appends `FORMAT Native` — incompatible. The handle therefore uses a separate `httpx.AsyncClient` for impersonated queries, posting to ClickHouse's HTTP endpoint with `?default_format=JSONEachRow` as a URL parameter (which the server *does* honor). `query_as_service` and the admin/audit methods keep using `clickhouse-connect`. As a consequence, `query_as_user` returns `list[dict[str, Any]]` (parsed JSON Lines) rather than a `QueryResult` — types are preserved by JSON encoding (ints stay ints, strings stay strings) but column-type metadata is lost. Routes needing column types should use the admin handle's `query_as_service`. Named parameters work via `param_<name>=<value>` URL params translated from the `parameters=` kwarg.

`init_user_rights` runs on every successful login (form submit or OAuth callback) via a generic post-login hook list at `app.state.post_login_hooks`, populated by `iris.clickhouse.install(app)`. Subsequent cookie-based session refreshes do NOT re-provision. Group changes between two logins are reconciled. If ClickHouse is unreachable at boot, `ensure_service_admin` raises and the app refuses to start; if it's unreachable mid-life, the post-login hook raises and the user gets a 500.

**iris's liveness is tied to ClickHouse's.** This is intentional: iris is a thin layer in front of ClickHouse, and a logged-in user with no ability to reach the data backend can't accomplish anything useful. Rather than hide that with best-effort provisioning, login fails loud when CH is down — operators see the exact failure mode in the access logs, monitoring catches it, and users get a real error rather than a half-broken session that errors on every subsequent query. If you want iris to remain available for non-CH features during a CH outage, set `IRIS_NO_CLICKHOUSE=1` (which skips the bridge install entirely) — there is no middle ground where the bridge is partially installed.

The `clickhouse_admin` role is a regular role in the authz mapping (created automatically by the bootstrap on first install, seeded as an include of the bootstrap admin role). Operators map it to whichever IdP groups they want via the mutator API; the role name itself is a `Final` constant `iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE = "clickhouse_admin"` and not env-configurable.

`build_app(install_clickhouse=False)` skips the bridge entirely — used by auth tests that don't need a CH testcontainer. Set `IRIS_NO_CLICKHOUSE=1` to disable the bridge in the module-level `app = build_app()` (used by `uv run iris` for local dev when CH isn't running).

### Per-database admin tier

Three tiers of CH authorization, in increasing privilege:

1. **Any logged-in user** — `get_clickhouse_handle` returns a `ClickHouseHandle` that runs impersonated SELECTs.
2. **Per-database admin** — `require_clickhouse_database_admin` returns a `ClickHouseDatabaseAdminHandle` scoped to one database. Methods cover `grant_select_to_user/group`, `revoke_select_from_user/group`, `add_row_policy_for_user/group`, `revoke_row_policy_for_user/group`, `add/remove_admin_user`, `add/remove_admin_role` (delegation), plus listing/audit. The dep takes `database: str` as a regular FastAPI parameter that gets bound from the calling route's path/query. A user is admin of a database if they're listed in `clickhouse_database_admins_users` for that DB, or any of their effective roles is listed in `clickhouse_database_admins_roles`. Global admins (`clickhouse_admin`) short-circuit to admin-of-everything.
3. **Global admin** — `require_clickhouse_admin` returns the existing `ClickHouseAdminHandle`. Strict superset of the per-DB tier.

A separate role gates **database creation**: `require_clickhouse_database_creator` returns a `ClickHouseDatabaseCreatorHandle` whose only method, `create_database(name)`, runs `CREATE DATABASE IF NOT EXISTS` and atomically records the calling user in `clickhouse_database_admins_users`. The bootstrap creates the empty `clickhouse_database_creator` role on first install but does NOT include it in the bootstrap admin role — operators decide via the mutator API.

Two new SQLite tables live in the same `AUTH_DB_PATH` file:

```sql
CREATE TABLE clickhouse_database_admins_users (
    database_name  TEXT NOT NULL,
    username_lower TEXT NOT NULL,
    PRIMARY KEY (database_name, username_lower)
);
CREATE TABLE clickhouse_database_admins_roles (
    database_name  TEXT NOT NULL,
    role_name      TEXT NOT NULL,
    PRIMARY KEY (database_name, role_name)
);
```

The `DatabaseAdminStore` class wraps these tables. It's installed by `iris.clickhouse.install` and exposed on `app.state.clickhouse_database_admins`. Routes that need to mutate per-DB admin assignments should obtain a session+database-scoped mutator via `store.for_session(session, database=...)`; the mutator re-checks `is_admin` before each call. Bare store methods are reserved for internal trusted code (the creator/admin handles, install, fixtures).

Example routes:

```python
@app.post("/clickhouse/databases/{database}")
async def create_database(
    database: str,
    handle: ClickHouseDatabaseCreatorHandle = Depends(require_clickhouse_database_creator),
):
    await handle.create_database(database)
    return {"created": database}


@app.post("/clickhouse/databases/{database}/grants/users/{username}")
async def grant_read(
    database: str,
    username: str,
    handle: ClickHouseDatabaseAdminHandle = Depends(require_clickhouse_database_admin),
):
    await handle.grant_select_to_user(username)
    return {"granted": True}


@app.post("/clickhouse/databases/{database}/admins/users/{username}")
async def delegate_admin(
    database: str,
    username: str,
    handle: ClickHouseDatabaseAdminHandle = Depends(require_clickhouse_database_admin),
):
    await handle.add_admin_user(username)
    return {"ok": True}
```

**Pre-existing target user constraint.** Grants and row policies target `<username>_USER` (or `<group>_GRP`) roles. These exist only after the user/group has been provisioned, which happens at login time via the existing `init_user_rights` post-login hook. Granting access to a user who has never logged in raises a CH error; the user must authenticate at least once first.

### Tests

The test suite uses `testcontainers-python` to spin up `clickhouse/clickhouse-server:26.3` in Docker. The container is session-scoped (one instance per pytest run); per-test isolation comes from a UUID-derived `prefix` fixture that namespaces every entity name. Docker is required to run `tests/clickhouse/`.

The `chdb` library was originally trialed for in-process testing; `chdb==4.1.6`'s embedded server hardcodes `system.user_directories` to a read-only `users_xml` entry, blocking all RBAC DDL at runtime. See the design spec at `docs/superpowers/specs/2026-05-05-clickhouse-authz-design.md` for the verification.

### Deferred (v1.1+)

- Connection pooling and multi-worker session sharing — `clickhouse-connect`'s `Client` is per-process today; multi-worker deploys would need a connection pool.
- A streaming variant of `query_as_user` for routes that need to stream large result sets back through Datastar SSE without buffering the whole response in memory.
