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

The `iris.auth` package adds session-based authentication and tier-based authorization to all routes. Public surface:

```python
from iris.auth import (
    AuthSession,                       # the dataclass returned by every auth dep
    Rights, EMPTY_RIGHTS,              # the rights view + a useful default
    Session, SessionOptional,          # auth-only aliases
    SessionAdmin,                      # global admin
    SessionDatabaseCreator,            # admin OR can_create_database
    SessionDatabaseAdmin,              # admin of the path's `database` parameter
    SessionWrite, SessionRead,         # tier-scoped checks against `database`
    User, install, bootstrap_admin,
)
```

Routes consume the dep aliases as type annotations — no `= Depends(...)` is needed:

```python
@app.get("/me")
async def me(session: Session) -> dict:
    return {"username": session.user.username}

@app.get("/db/{database}/read")
async def read_db(database: str, session: SessionRead) -> ...:
    ...

@app.post("/db/{database}/grants/users/{username}")
async def grant_read(database: str, username: str, session: SessionDatabaseAdmin) -> ...:
    ...
```

`AuthSession` exposes `id`, `user` (a `User`), `created_at`, `expires_at`, `data` (the per-session mutable dict), and `rights` (a frozen `Rights` view). The `data` field is the same dict object as the session store's storage, so `session.data[key] = value` writes through with no commit step. All other fields are frozen. There is no `roles` field; templates that want the IdP groups read `session.user.groups`.

**One type, seven deps.** Every auth-flavored route parameter has the same uniform shape: `session: <Alias>` where `<Alias>` is one of the seven aliases below. The choice of alias determines the access-control policy:

| Alias | Admits when | Raises |
|---|---|---|
| `Session` | any logged-in user | 401 with no session |
| `SessionOptional` | any caller (returns `None` if no session) | never |
| `SessionAdmin` | `session.rights.is_admin` | 401 / 403 |
| `SessionDatabaseCreator` | admin or `can_create_database` | 401 / 403 |
| `SessionDatabaseAdmin` | admin or `db_admin[database]` | 401 / 403 |
| `SessionWrite` | admin or `db_admin[database]` or `db_writer[database]` | 401 / 403 |
| `SessionRead` | admin or any tier on `database` | 401 / 403 |

The three database-scoped aliases (`SessionRead`/`SessionWrite`/`SessionDatabaseAdmin`) read `database: str` from the calling route's path or query parameters via FastAPI's normal binding. A typo'd or missing role configuration is no longer a 500 case — rights come from CH at login, and any check just compares against the cached `Rights` value.

### Per-session server-side data

Each `UserSession` carries a mutable `data: dict[str, Any]` field for arbitrary route-managed state (drafts, wizard steps, recently-viewed lists, etc.). Every alias dep exposes it via `session.data`:

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
        "is_admin": session.rights.is_admin,
        "groups": list(session.user.groups),
    }
```

- `session.data` is a per-request snapshot — a fresh `dict` deserialized from the SQLite row on every request. Mutations to the dict do **not** auto-persist; routes that want the change to survive call `await request.app.state.auth_session_store.update_data(session.id, session.data)` before returning.
- `AuthSession` exposes `id`, `user`, `created_at`, `expires_at`, `data`, and `rights` on a single value. Routes that need only the user write `session.user`; routes that need the per-session bag write `session.data`; routes that need the authorization view write `session.rights`.

Lifecycle: `data` is JSON-encoded into the SQLite row alongside the session. Mutations are persisted by `update_data` and survive process restarts. Values must be JSON-encodable (strings, ints, floats, bools, `None`, lists, dicts) — anything else raises `TypeError` at write time. Read-modify-write across an `await` between two requests for the same session has the standard interleaving race; acceptable at ≤20-user scale, document or use `asyncio.Lock` if a route needs atomic updates.

### Authorization (CH-derived rights)

ClickHouse is the only source of truth for authorization. There is no SQLite role mapping, no `authz_*` tables, no `RoleMappingStore`, no per-database admin store. Iris derives a frozen `Rights` view from CH grants once at login (in the post-login hook chain) and caches it on the session row; routes gate via the alias deps in `iris.auth.deps`, which inspect `session.rights`.

**The `Rights` shape:**

```python
@dataclass(frozen=True, slots=True)
class Rights:
    is_admin: bool                          # global admin
    can_create_database: bool               # CREATE DATABASE on *.*
    db_admin: frozenset[str]                # databases with full delegation power
    db_writer: frozenset[str]               # databases with SELECT+INSERT+ALTER UPDATE
    db_reader: frozenset[str]               # databases with SELECT
```

`Rights` exposes three helpers — `has_read(database)`, `has_write(database)`, `has_admin(database)` — each using the implied tier ordering (`is_admin` ⊇ `db_admin[X]` ⊇ `db_writer[X]` ⊇ `db_reader[X]`).

**How rights are derived.** At login, `iris.clickhouse.rights.derive_rights(client, username, groups)`:

1. Walks `system.role_grants` transitively to collect the user's effective role set (starting from `<username>_USER` plus each `<group>_GRP`).
2. Splits any role ending in `_DBADMIN`, `_DBWRITER`, `_DBREADER` to recover the database name and populates the corresponding `frozenset`.
3. Queries `system.grants` filtered to the effective role set:
   - `is_admin = True` if some role holds `ROLE ADMIN` at global scope (`database IS NULL`) with `grant_option=1`. CH always expands `GRANT ALL` into primitive privileges, so `access_type='ALL'` never appears — ROLE ADMIN+WGO is the stable single-row marker.
   - `can_create_database = True` if some role holds `CREATE DATABASE` at global scope (no GRANT OPTION required).

Operator changes (new tier-role grants, revocations) take effect on the user's next login. There is no mid-session re-derivation. To force a re-derive operationally: revoke the grant in CH (so any actual query fails) and delete the user's session rows from the auth DB.

**The CH-side storage.** Per-database tier roles, created at database creation and dropped at deletion:
- `<X>_DBADMIN` — `GRANT ALL ON X.* WITH GRANT OPTION`
- `<X>_DBWRITER` — `GRANT SELECT, INSERT, ALTER UPDATE ON X.*`
- `<X>_DBREADER` — `GRANT SELECT ON X.*`

The per-user (`<username>_USER`) and per-group (`<group>_GRP`) roles continue to be created lazily by `init_user_rights` on each login. They serve as the recipients of tier-role grants: `add_writer(bob)` runs `GRANT <X>_DBWRITER TO bob_USER`.

**Bootstrap** (option β). On app boot, after `ensure_service_admin`, if `IRIS_BOOTSTRAP_USER` is set and no iris user role currently holds the admin marker (ROLE ADMIN+WGO at global scope, `_USER`-suffixed), iris seeds `<username>_USER` with `GRANT ALL ON *.* WITH GRANT OPTION`. Idempotent: re-runs with an existing admin are no-ops. Wiping the CH server re-triggers the seed.

The detection is restricted to roles ending in `_USER` so iris's own service identity (which necessarily holds ROLE ADMIN+WGO to manage RBAC state) is never mistaken for a bootstrapped admin.

**Pre-create-on-grant** is preserved as a username-enumeration defense. Tier-grant helpers (`grant_tier_to_user`, `grant_tier_to_group`) issue `CREATE ROLE IF NOT EXISTS <target>_USER` before granting, so an admin granting access to a never-logged-in user gets the same CH response as for an existing user.

**Identity matching:**
- `groups` — exact, case-sensitive match against `User.groups` (verbatim from the IdP). The `<group>_GRP` role in CH is named after the group string.
- `users` — `<username>_USER` — case-sensitive match against `User.username` (the CH role name uses the literal username).
  - OAuth provider sources `username` from the `preferred_username` claim, falling back to `sub` if absent.
  - LDAP provider sources `username` from the `username` substituted into `LDAP_BIND_DN_TEMPLATE`.
  - Mock provider sources `username` from `MOCK_USERNAME`.

**Use in routes:** see the alias table in the section above. `require_role(name)` and `RoleMappingStore` are gone; `SessionAdmin` / `SessionRead` / etc. cover all the previous role-gating use cases. The `clickhouse_admin` role concept disappears — iris admin and CH admin are the same `is_admin` flag, derived from CH.

For the full design and rationale, see `docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md`.

### Configuration

Env vars are loaded at `import iris` time via `python-dotenv`. If a `.env` file exists at the project root (gitignored), its values populate `os.environ` for any keys not already set. If `.env` is absent it's a silent no-op. **Real shell env vars take precedence over `.env`** (`load_dotenv` is called with `override=False`), so a CI / production deploy can override individual values without editing `.env`. Tests inherit the same loader; `tests/conftest.py` sets `os.environ.setdefault(...)` defaults at module scope before iris is imported, so test runs always end up with `AUTH_METHOD=mock` regardless of what `.env` contains — this protects the test suite from a developer's OAuth/LDAP `.env`.

Selected by env var. Per-deployment toggle: only one method is active at a time.

```
AUTH_METHOD=oauth | ldap | mock
SESSION_COOKIE_NAME=iris_session
SESSION_TTL_SECONDS=43200            # 12h, sliding TTL refreshed on each request
SESSION_ABSOLUTE_TTL_SECONDS=2592000 # 30d, hard cap on top of sliding TTL
SESSION_MAX_PER_USER=10              # cap concurrent sessions per User.subject (oldest evicted)
AUTH_DB_PATH=./iris-auth.db          # SQLite file backing the session store; :memory: for tests
COOKIE_SECURE=true                   # set false for local dev over http
IRIS_BOOTSTRAP_USER=                 # if set, seeded as the first CH admin on a fresh CH

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
├── __init__.py        # public surface: AuthSession, Rights, EMPTY_RIGHTS, Session, SessionOptional,
│                      #                  SessionAdmin, SessionDatabaseCreator, SessionDatabaseAdmin,
│                      #                  SessionWrite, SessionRead, User, install, bootstrap_admin
├── session.py         # Rights frozen dataclass + serialization helpers + EMPTY_RIGHTS constant
├── identity.py        # User (frozen+slots), UserSession (mutable; internal), AuthSession (frozen view)
├── config.py          # AuthSettings.from_env() — reads IRIS_BOOTSTRAP_USER, AUTH_METHOD, etc.
├── sessions.py        # SessionStore (SQLite): create / get_and_refresh / update_data / set_rights / delete / close
├── exceptions.py      # AuthRequired, AuthForbidden, AuthError + install_exception_handlers
├── deps.py            # the seven Annotated alias deps + set_session_store / set_settings
├── csrf.py            # double-submit CSRF
├── rate_limit.py      # TokenBucket (used on POST /login)
├── routes.py          # /login, /login/callback, /logout, /api/whoami; install(app)
├── bootstrap.py       # bootstrap_admin: first-install CH admin seed (option β)
└── providers/         # mock, ldap, oauth — unchanged
```

`install(app)` reads env, builds the provider, and wires the auth router + exception handlers + session store into a FastAPI app. Called from `build_app()` in `src/iris/app.py`.

### Authorization model

ClickHouse RBAC is the single source of truth. Iris does not maintain a separate role mapping. Routes guard themselves with the alias deps from `iris.auth.deps`; the seven aliases cover every previous gating use case (any-auth, optional, global admin, database creator, per-DB admin, per-DB writer, per-DB reader). See "Authorization (CH-derived rights)" above for derivation, the bootstrap, and CH-side storage.

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
- `derive_rights` runs a small handful of CH queries at login (role-grants walk + a single grants enumeration). Sub-millisecond at ≤20-user scale; for higher request volumes, profile and consider caching the effective role set per user with a CH version-column invalidation.

These are accepted residual risks for the ≤20-user deploy profile; revisit when scaling out or relocating behind a load balancer.

## ClickHouse

The `iris.clickhouse` package provisions ClickHouse users, roles, grants, and row policies, provides audit-query helpers, and (via the bridge submodule) hands FastAPI routes typed handles for impersonated/admin queries. The plain-data helpers (`audit.py`, `bootstrap.py`, `client.py`, `grants.py`, `policies.py`, `users.py`) are independent of `iris.auth`; only `deps.py` and `install.py` import from auth.

### Public surface

```python
from iris.clickhouse import (
    # plain-data helpers
    ClickHouseSettings, build_client, ensure_service_admin,
    init_user_rights, derive_rights,
    grant_select_to_database, grant_insert_update_to_table,
    add_row_policy, revoke_row_policy,
    # tier-role helpers
    TIER_DBADMIN, TIER_DBWRITER, TIER_DBREADER,
    create_tier_roles, drop_tier_roles, tier_role_name,
    grant_tier_to_user, grant_tier_to_group,
    revoke_tier_from_user, revoke_tier_from_group,
    # audit helpers
    user_grants, role_grants, user_role_memberships,
    user_row_policies, role_row_policies, table_row_policies,
    # FastAPI lifecycle
    install,
)
```

The per-tier method surface lives on the Session subclasses in `iris.auth.identity` (see "Auth ↔ ClickHouse bridge" below). `iris.clickhouse` itself no longer hosts FastAPI handle providers.

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

Routes consume one alias dep per tier. The dep returns a Session subclass whose method surface matches the tier; there is no separate handle parameter. The Session value carries both admission (alias gates the request) and capability (the subclass exposes only its tier's methods).

```python
from iris.auth import Session, SessionAdmin, SessionRead, SessionDatabaseAdmin

@app.get("/db/{database}/count")
async def count(database: str, session: SessionRead):
    return await session.query_as_user("SELECT count() FROM t")

@app.post("/db/{database}/grants/users/{username}")
async def grant_read(database: str, username: str, session: SessionDatabaseAdmin):
    await session.grant_reader(username)
    return {"granted": True}

@app.get("/admin/users/{username}/grants")
async def audit(username: str, session: SessionAdmin):
    return await session.user_grants(username=username)
```

`SessionRead` returns a `DatabaseSession` bound to the path's `database`. `query_as_user("SELECT count() FROM t")` resolves `t` against `<database>` because the impersonated request includes `?database=<database>` in the URL. There's no separate handle parameter; the Session value carries both the admission decision and the CH-method surface.

For routes that need to query a specific database from a non-DB-scoped session (`Session` or `SessionAdmin`), `query_as_user` accepts a `database=` kwarg. `SessionAdmin.query_as_service` likewise accepts `database=`.

**Why two HTTP transports.** `query_as_user` prepends `EXECUTE AS <quoted_username>` to the SQL. ClickHouse's `EXECUTE AS user <SELECT>` body grammar rejects `FORMAT` clauses, but `clickhouse-connect`'s `query()` always appends `FORMAT Native` — incompatible. The Session methods therefore use a separate `httpx.AsyncClient` for impersonated queries, posting to ClickHouse's HTTP endpoint with `?default_format=JSONEachRow` as a URL parameter. Service-identity queries (`query_as_service`) and admin/audit methods keep using `clickhouse-connect`. As a consequence, `query_as_user` returns `list[dict[str, Any]]` (parsed JSON Lines) rather than a `QueryResult` — types are preserved by JSON encoding but column-type metadata is lost. Routes needing column types use `AdminSession.query_as_service`. Named parameters work via `param_<name>=<value>` URL params translated from the `parameters=` kwarg.

**Post-login hook chain.** `iris.clickhouse.install(app)` registers a hook on `app.state.post_login_hooks` that fires on every successful login (form submit or OAuth callback). The hook does two things in order: `init_user_rights` (provisions the CH user/role/group memberships) and `derive_rights` (computes the `Rights` view), then `set_rights` persists the rights to the session row. Cookie-based session refreshes do NOT re-provision; the cached `Rights` is what every subsequent request sees. Group changes between two logins are reconciled.

**iris's liveness is tied to ClickHouse's.** This is intentional: iris is a thin layer in front of ClickHouse, and a logged-in user with no ability to reach the data backend can't accomplish anything useful. Rather than hide that with best-effort provisioning, login fails loud when CH is down — operators see the exact failure mode in the access logs, monitoring catches it, and users get a real error rather than a half-broken session that errors on every subsequent query.

`build_app(install_clickhouse=False)` skips the bridge entirely — used by auth tests that don't need a CH testcontainer. With CH disabled, the post-login hook chain is empty and sessions land with `EMPTY_RIGHTS` and `client=None`/`http_client=None`. Calling a CH method on such a session raises (the `httpx.AsyncClient.post` on `None` errors); tests that don't exercise CH simply don't call them. Production launches via uvicorn factory mode (`uvicorn.run("iris.app:build_app", factory=True, ...)`), so importing `build_app` is side-effect-free for tests.

**Session implementation.** The Session classes (`AuthSession`, `DatabaseSession`, `DatabaseAdminSession`, `DatabaseCreatorSession`, `AdminSession`) live in `iris.auth.identity`. Each method lazy-imports the matching `*_impl` async function from `iris.clickhouse.handle` and calls it with `self.client` / `self.http_client` / `self.user.username` / (for DB-scoped subclasses) `self.database`. The lazy import avoids an `iris.auth → iris.clickhouse` cycle at module load.

### Per-database admin tier

Per-database admin is a CH role membership: a user is admin of database `X` iff their effective role set (transitively) includes `<X>_DBADMIN`. There is no separate SQLite admin store. The same applies to writer (`<X>_DBWRITER`) and reader (`<X>_DBREADER`). The Session aliases map to tier-typed Session subclasses returned by the dep:

| Tier | Alias | Returns | Selected methods |
|---|---|---|---|
| Any logged-in user | `Session` | `AuthSession` | `query_as_user(sql, database=None)` |
| Database creator | `SessionDatabaseCreator` | `DatabaseCreatorSession` | `create_database(name)` |
| Per-database admin | `SessionDatabaseAdmin` | `DatabaseAdminSession` (bound to `database` from path) | `grant_reader/writer`, `add_admin_user`, `revoke_*`, `delete_database`, `list_admin_members`, `list_grants`, `list_row_policies` |
| Global admin | `SessionAdmin` | `AdminSession` | `query_as_service`, `reprovision_user`, `add/revoke_row_policy`, audit (`user_grants`, `role_grants`, `user_role_memberships`, `user_row_policies`, `role_row_policies`, `table_row_policies`) |

`create_database(name)` is the lifecycle entry point: it runs `CREATE DATABASE IF NOT EXISTS`, creates the three tier roles (`<name>_DBADMIN`, `<name>_DBWRITER`, `<name>_DBREADER`) with their privilege grants, and grants `<name>_DBADMIN` to the creator's `<creator>_USER` role. All steps idempotent. `delete_database()` reverses: `DROP DATABASE IF EXISTS` then drops the three tier roles.

There is no separate `clickhouse_database_creator` CH role. The capability is the `can_create_database` flag on `Rights`, derived from `GRANT CREATE DATABASE ON *.*` on any role in the user's effective set. Global admins also satisfy `SessionDatabaseCreator` via the `is_admin` superset.

A global admin who needs to do per-DB operations writes routes gated by `SessionDatabaseAdmin` (which admits admins via the `is_admin` superset and returns a `DatabaseAdminSession` bound to the path's database). Routes that need both global ops and per-DB ops compose two Session parameters; this is rare.

Example routes:

```python
@app.post("/clickhouse/databases/{database}")
async def create_database(database: str, session: SessionDatabaseCreator):
    await session.create_database(database)
    return {"created": database}


@app.post("/clickhouse/databases/{database}/grants/users/{username}")
async def grant_read(database: str, username: str, session: SessionDatabaseAdmin):
    await session.grant_reader(username)
    return {"granted": True}


@app.post("/clickhouse/databases/{database}/admins/users/{username}")
async def delegate_admin(database: str, username: str, session: SessionDatabaseAdmin):
    await session.add_admin_user(username)
    return {"ok": True}


@app.delete("/clickhouse/databases/{database}")
async def delete_database(database: str, session: SessionDatabaseAdmin):
    await session.delete_database()
    return {"deleted": database}
```

**Pre-create-on-grant for username enumeration.** Granting a tier role to a user who has never logged in is supported: tier-grant helpers issue `CREATE ROLE IF NOT EXISTS <target>_USER` before granting, so the CH response is the same whether the target has authenticated or not. Once the target eventually logs in, `init_user_rights` reuses the existing role and `derive_rights` picks up the tier membership.

### Tests

The test suite uses `testcontainers-python` to spin up `clickhouse/clickhouse-server:26.3` in Docker. The container is session-scoped (one instance per pytest run); per-test isolation comes from a UUID-derived `prefix` fixture that namespaces every entity name. Docker is required to run `tests/clickhouse/`.

The `chdb` library was originally trialed for in-process testing; `chdb==4.1.6`'s embedded server hardcodes `system.user_directories` to a read-only `users_xml` entry, blocking all RBAC DDL at runtime. See the design spec at `docs/superpowers/specs/2026-05-05-clickhouse-authz-design.md` for the verification.

### Deferred (v1.1+)

- Connection pooling and multi-worker session sharing — `clickhouse-connect`'s `Client` is per-process today; multi-worker deploys would need a connection pool.
- A streaming variant of `query_as_user` for routes that need to stream large result sets back through Datastar SSE without buffering the whole response in memory.
