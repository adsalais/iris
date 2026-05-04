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
from iris.auth import CurrentUser, OptionalCurrentUser, CurrentSession, SessionData, CurrentRoles, require_role
```

`CurrentUser` requires a valid session (cookie or `Authorization: Bearer <session-id>`); `OptionalCurrentUser` returns `None` if no session is present. `require_role("admin")` is a dependency factory that 403s if the user's effective role set (computed from the role-mapping YAML) doesn't contain the named role. See "Authorization (roles)" below for the YAML schema and inheritance semantics.

### Per-session server-side data

Each `UserSession` carries a mutable `data: dict[str, Any]` field for arbitrary route-managed state (drafts, wizard steps, recently-viewed lists, etc.). Two FastAPI deps expose it:

```python
from iris.auth import SessionData, CurrentSession

@app.post("/draft")
async def save_draft(data: SessionData, body: dict):
    data["draft"] = body          # direct mutation persists; no commit step
    return {"ok": True}

@app.get("/draft")
async def get_draft(data: SessionData):
    return data.get("draft", {})

@app.get("/me/full")
async def me_full(session: CurrentSession):
    return {"id": session.id, "logged_in_at": session.created_at, "data_keys": list(session.data)}
```

- `SessionData` returns the dict directly — for routes that only need to read/write keys.
- `CurrentSession` returns the whole `UserSession` — for routes that need `id`, `created_at`, `expires_at`, `user`, or all of the above plus data.

All four authenticated deps (`CurrentUser`, `OptionalCurrentUser`, `CurrentSession`, `SessionData`) share a single `_resolve_session` lookup per request via FastAPI's dep cache, so a route taking both `user: CurrentUser` and `data: SessionData` makes one store hit, not two.

Lifecycle: data lives in-memory alongside the session and is wiped on logout / expiry / process restart. The store API doesn't persist `data` separately; if/when the v1.1 Redis-backed store arrives, `data` will need to be serializable (JSON-ish values only). For v1, anything Python can hold is fair game. Read-modify-write across an `await` between two requests for the same session has the standard interleaving race — acceptable at ≤20-user scale; document or use `asyncio.Lock` if a route needs atomic updates.

### Authorization (roles)

Application code references **internal role names only** (`admin`, `writer`, `reader`, etc.). The mapping from role → external IdP groups/usernames lives in a YAML file outside the code, edited by operators without a redeploy.

**YAML schema** (single file, path from `AUTHZ_CONFIG_PATH`):

```yaml
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: ["editors"]
    users: ["bob"]
    includes: ["reader"]            # writers also have reader's permissions
  admin:
    groups: ["ldap-admins", "platform-team"]
    users: ["alice"]
    includes: ["writer"]            # admins transitively get writer + reader
```

Validation rules (enforced at load, fail-loud with line numbers):
- Top-level: exactly `roles:`. Other keys reject.
- Per-role keys restricted to `{groups, users, includes}`; all default to `[]`.
- Role names match `[a-zA-Z0-9_-]+`.
- `includes` must reference defined roles; the graph must be a DAG (cycles reject).
- Duplicate top-level role keys reject (custom YAML loader; PyYAML's default would silently overwrite).

**Identity matching:**
- `groups` — exact, case-sensitive match against `User.groups` (verbatim from the IdP).
- `users` — case-insensitive match against `User.username` (the new field on `User`).
  - OAuth provider sources `username` from the `preferred_username` claim, falling back to `sub` (the IdP's opaque subject identifier) if absent. If your OIDC IdP doesn't issue `preferred_username`, your `users:` lists must contain the `sub` UUIDs.
  - LDAP provider sources `username` from the `username` substituted into `LDAP_BIND_DN_TEMPLATE`.
  - Mock provider sources `username` from `MOCK_USERNAME`.

**Use in routes:**

```python
from iris.auth import require_role, CurrentRoles, CurrentUser

@app.get("/docs")
async def list_docs(user: User = Depends(require_role("reader"))):
    ...

@app.get("/me/roles")
async def my_roles(roles: CurrentRoles):
    return {"roles": sorted(roles)}
```

`require_role("reader")` admits any user whose effective role set contains `reader`, directly or via `includes` (so admins and writers get in too). `CurrentRoles` returns the user's full effective role set as a `frozenset[str]` — useful for templates and `/api/whoami`-style endpoints.

If a route names a role that isn't defined in the YAML, the request returns **500** (not 403) with a generic body — silent 403s would mask operator typos like `require_role("reder")`. The missing role name is logged server-side.

**Live reload:** the loader stats the YAML file on every request and reloads when mtime changes. Edit the file, save, and the next request sees the new mapping — no restart, no waiting for sessions to expire.

**Robustness against bad edits:** if a YAML edit produces an invalid file (syntax error, schema error, cycle, undefined include), the loader logs an `ERROR` and **keeps serving the last-known-good mapping**. Subsequent requests keep working until the file is fixed. Note that the *initial* load at boot is not protected by this fallback — a bad initial YAML stops the app from booting (consistent with the rest of the auth config's fail-loud style).

### Configuration

Env vars are loaded at `import iris` time via `python-dotenv`. If a `.env` file exists at the project root (gitignored), its values populate `os.environ` for any keys not already set. If `.env` is absent it's a silent no-op. **Real shell env vars take precedence over `.env`** (`load_dotenv` is called with `override=False`), so a CI / production deploy can override individual values without editing `.env`. Tests inherit the same loader; `tests/conftest.py` sets `os.environ.setdefault(...)` defaults at module scope before iris is imported, so test runs always end up with `AUTH_METHOD=mock` regardless of what `.env` contains — this protects the test suite from a developer's OAuth/LDAP `.env`.

Selected by env var. Per-deployment toggle: only one method is active at a time.

```
AUTH_METHOD=oauth | ldap | mock
SESSION_COOKIE_NAME=iris_session
SESSION_TTL_SECONDS=43200            # 12h, sliding TTL refreshed on each request
SESSION_ABSOLUTE_TTL_SECONDS=2592000 # 30d, hard cap on top of sliding TTL
SESSION_MAX_PER_USER=10              # cap concurrent sessions per User.subject (oldest evicted)
COOKIE_SECURE=true                   # set false for local dev over http
AUTHZ_CONFIG_PATH=./authz.yaml       # role mapping; required, fail-loud if unset

# OAuth (OIDC discovery)
OIDC_ISSUER_URL=https://keycloak.example.com/realms/iris
OIDC_CLIENT_ID=iris
OIDC_CLIENT_SECRET=...
OIDC_SCOPES=openid profile email groups

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

### Deployment constraint: single worker only

`InMemorySessionStore` is per-process: each uvicorn worker has its own store. Running with `uvicorn --workers >1` will silently break sessions (a request's cookie may hit a worker that doesn't know the session, manifesting as a logged-in user being redirected to `/login`). For ≤20 users this is fine — keep the deploy at `--workers 1`. To go beyond, swap the store for a Redis/DB-backed implementation; the API surface is small enough (`create` / `get_and_refresh` / `delete`) that it's a focused change.

### Module map

```
src/iris/auth/
├── __init__.py        # public surface: CurrentUser, OptionalCurrentUser, CurrentSession, SessionData, CurrentRoles, require_role, install, User, UserSession
├── config.py          # AuthSettings.from_env() + per-method sub-settings
├── identity.py        # User (frozen+slots), UserSession (mutable for sliding TTL)
├── sessions.py        # InMemorySessionStore: create / get_and_refresh / delete
├── exceptions.py      # AuthRequired, AuthForbidden, AuthError, AuthorizationMisconfigured + install_exception_handlers
├── deps.py            # CurrentUser, OptionalCurrentUser, CurrentSession, SessionData, set_session_store, set_settings
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
    ├── __init__.py    # (empty package marker; consumers import from `iris.auth`)
    ├── config.py      # AuthzSettings.from_env() — reads AUTHZ_CONFIG_PATH
    ├── mapping.py     # RoleMapping value type + parse() with cycle detection + closure
    ├── loader.py      # RoleMappingLoader: mtime-cached, last-good fallback on bad reload
    └── deps.py        # require_role(name) factory; CurrentRoles dep; _current_mapping
```

`install(app)` reads env, builds the provider, and wires the auth router + exception handlers + session store into a FastAPI app. Called from `build_app()` in `src/iris/app.py`.

### Authorization model

Roles are internal names (`admin`, `writer`, `reader`, …) defined in the YAML at `AUTHZ_CONFIG_PATH`, mapped to external IdP groups and/or usernames. Routes guard themselves with `Depends(require_role("admin"))` — they never reference IdP group names directly. Operators edit the YAML to (re)map roles to whatever the deployment's IdP exposes; no code change needed when group names differ across OAuth and LDAP. See "Authorization (roles)" above for the schema and inheritance semantics.

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

### Open redirect protection

`_safe_next(url)` accepts only same-origin relative paths. Rejects empty, non-`/`-prefixed, `//`-prefixed (protocol-relative), absolute URLs, and backslash-containing strings (browsers normalize `\` → `/` before same-origin checks). Applied at `POST /login` and `GET /login/callback`. Failure-redirect URLs are constructed via `urllib.parse.urlencode` so error tokens or path components can't break query parsing.

### Open security follow-ups (v1.1)

- `InMemorySessionStore` is per-process, which forces `--workers 1` (see "Deployment constraint" above). Swapping to a Redis/DB-backed store would lift the constraint and also survive process restarts.
- Rate limiting on `POST /login` keys on `request.client.host`. Behind a reverse proxy this is the proxy's IP — the bucket becomes effectively global. Mitigation: run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>` so `request.client.host` reflects the `X-Forwarded-For` value. Not enforced; deployment-config concern.
- `OAuthProvider` caches the IdP's JWKS once on first discovery. If the IdP rotates signing keys, all logins fail until iris is restarted. Acceptable at ≤20-user / multi-month rotation cadence; tighten by re-fetching on `kid`-not-in-set if rotation matters.
- OIDC discovery is now lazy: the *first* login attempt after restart pays the discovery latency. Acceptable for v1, but means a slow IdP shifts startup latency to a request boundary instead of failing loud at boot.
- The role-mapping loader stats the YAML on every request. Sub-millisecond at ≤20-user scale; for higher request volumes, swap to a file watcher (e.g., `watchfiles`) or event-driven invalidation.

These are accepted residual risks for the ≤20-user / `--workers 1` deploy profile; revisit when scaling out or relocating behind a load balancer.
