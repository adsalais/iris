# Authentication

The `iris.auth` package adds session-based authentication and tier-based
authorization to all routes.

## Public surface

```python
from iris.auth import (
    AuthSession,                       # base session type
    Capabilities, EMPTY_CAPABILITIES,  # the capabilities view + a useful default
    Session, SessionOptional,          # auth-only aliases
    SessionAdmin,                      # global admin
    SessionDatabaseCreator,            # admin OR can_create_database
    SessionDatabaseAdmin,              # admin of the path's `database` parameter
    SessionWrite, SessionRead,         # tier-scoped checks against `database`
    User, install,
    # subclass types also exported: AdminSession, DatabaseAdminSession, DatabaseCreatorSession, DatabaseSession
)
```

Routes consume the dep aliases as type annotations — no `= Depends(...)` needed:

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

## Alias deps

Every auth-flavored route parameter has the same uniform shape: `session: <Alias>`.
The choice of alias determines the access-control policy.

| Alias | Admits when | Raises | Return type |
|---|---|---|---|
| `Session` | any logged-in user | 401 with no session | `AuthSession` |
| `SessionOptional` | any caller | never | `AuthSession \| None` |
| `SessionAdmin` | `session.capabilities.is_admin` | 401 / 403 | `AdminSession` |
| `SessionDatabaseCreator` | admin or `can_create_database` | 401 / 403 | `DatabaseCreatorSession` |
| `SessionDatabaseAdmin` | admin or `db_admin[database]` | 401 / 403 | `DatabaseAdminSession` |
| `SessionWrite` | admin or `db_admin[database]` or `db_writer[database]` | 401 / 403 | `DatabaseSession` |
| `SessionRead` | admin or any tier on `database` | 401 / 403 | `DatabaseSession` |

The three database-scoped aliases (`SessionRead`/`SessionWrite`/`SessionDatabaseAdmin`)
read `database: str` from the calling route's path or query parameters via FastAPI's
normal binding. A typo'd or missing role configuration is no longer a 500 case —
capabilities come from CH at login, and any check compares against the cached `Capabilities` value.

A missing role name (the old `require_role("reder")` footgun) cannot happen here;
there are no role names at all — only tier membership sets.

## Session class hierarchy

All auth deps return a subclass of `AuthSession`. The subclass type is what pyright
sees on the route signature, so admin-only methods like `query_as_service` are only
callable on `AdminSession`, not on `AuthSession` or `DatabaseSession`. The
capability-bounds claim is real: route signatures get exactly the methods their tier
permits, enforced at type-check time.

`AuthSession` fields:

- `id` — session UUID
- `user` — a `User` (frozen+slots: `username`, `display_name`, `groups`, `subject`)
- `created_at` / `expires_at` — datetime, UTC
- `data` — mutable `dict[str, Any]` (per-session server-side bag; see next section)
- `capabilities` — a frozen `Capabilities` view (derived from CH at login; see "Authorization" below)

There is no `roles` field. Templates that want IdP groups read `session.user.groups`.

The CH method implementations (`query_as_user`, `query_as_service`, etc.) live in
`iris.clickhouse.queries`. The session subclasses in `iris.auth.views` import them at
module top level and delegate via `asyncio.to_thread` for the sync helpers.

## Per-session server-side data

Each session carries a mutable `data: dict[str, Any]` field for arbitrary
route-managed state (drafts, wizard steps, recently-viewed lists, etc.).

```python
from iris.auth import Session

@app.post("/draft")
async def save_draft(session: Session, body: dict):
    session.data["draft"] = body
    await session.persist_data()
    return {"ok": True}

@app.get("/draft")
async def get_draft(session: Session):
    return session.data.get("draft", {})
```

Key semantics:

- `session.data` is a **per-request snapshot** — a fresh `dict` deserialized from
  the SQLite row on every request. Mutations do **not** auto-persist.
- Routes that want the change to survive call `await session.persist_data()`
  before returning. The method is a thin wrapper that writes the current
  `session.data` back to the session store.
- Values must be JSON-encodable (`str`, `int`, `float`, `bool`, `None`, `list`,
  `dict`) — anything else raises `TypeError` at write time.
- Read-modify-write across an `await` between two concurrent requests for the same
  session has the standard interleaving race; acceptable at ≤20-user scale.

`data` is JSON-encoded into the SQLite row alongside the session and survives process
restarts.

## Authorization (CH-derived capabilities)

ClickHouse is the **only source of truth** for authorization. There is no SQLite role
mapping, no `authz_*` tables, no `RoleMappingStore`. Iris derives a frozen `Capabilities`
view from CH grants once at login and caches it on the session row; alias deps
inspect `session.capabilities`.

**The `Capabilities` dataclass:**

```python
@dataclass(frozen=True, slots=True)
class Capabilities:
    is_admin: bool                          # global admin
    can_create_database: bool               # CREATE DATABASE on *.*
    db_admin: frozenset[str]                # databases with full delegation power
    db_writer: frozenset[str]               # databases with SELECT+INSERT+ALTER UPDATE
    db_reader: frozenset[str]               # databases with SELECT
```

`Capabilities` exposes three helpers — `has_read(database)`, `has_write(database)`,
`has_admin(database)` — using the implied tier ordering
(`is_admin` ⊇ `db_admin[X]` ⊇ `db_writer[X]` ⊇ `db_reader[X]`).

**How `derive_capabilities` works.** At login,
`iris.clickhouse.capabilities.derive_capabilities(client, username, groups)`:

1. Walks `system.role_grants` transitively to collect the user's effective role set
   (starting from `<username>_USER` plus each `<group>_GRP`).
2. Splits any role ending in `_DBADMIN`, `_DBWRITER`, `_DBREADER` to recover the
   database name and populates the corresponding `frozenset`.
3. Queries `system.grants` filtered to the effective role set:
   - `is_admin = True` if some role holds `ROLE ADMIN` at global scope
     (`database IS NULL`) with `grant_option=1`. CH always expands `GRANT ALL` into
     primitive privileges, so `access_type='ALL'` never appears — ROLE ADMIN+WGO is
     the stable single-row admin marker.
   - `can_create_database = True` if some role holds `CREATE DATABASE` at global
     scope (no GRANT OPTION required).

The admin-detection check is restricted to roles ending in `_USER` or `_GRP` so
iris's own service connection (which necessarily holds ROLE ADMIN+WGO to manage RBAC
state) is never mistaken for a bootstrapped admin.

**Operator changes** (new tier-role grants, revocations) take effect on the user's
next login. There is no mid-session re-derivation. To force an immediate re-derive:
revoke the grant in CH and delete the user's session rows from the auth DB.

For the full design and rationale, see
`docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md`.

## Login flows

- **OAuth (`AUTH_METHOD=oauth`)** — `/login` 302s to the IdP authorize URL with
  PKCE S256 + state in a signed cookie. The IdP redirects back to `/login/callback`,
  which exchanges the code, verifies the returned `id_token` (RS256/ES256 signature
  against the IdP's JWKS, plus `iss`/`aud`/`exp` claims), fetches userinfo, and
  creates a session. JWKS is fetched once at app construction; rotating IdP keys
  requires app restart. `next` is preserved across the round-trip via the same
  signed cookie.
- **LDAP/Mock (`AUTH_METHOD=ldap`/`mock`)** — `/login` renders an HTML form
  (Jinja template `templates/auth/ldap_form.html`) with a CSRF token. `POST /login`
  validates CSRF, calls `provider.authenticate(username, password)`, and creates a
  session on success. Bad creds redirect back to
  `/login?error=invalid_credentials&next=...`.
- **Logout** — `POST /logout` (CSRF-required) deletes the session and clears the
  cookie. Local-only — does not call the IdP's end-session endpoint.

The CSRF cookie is rotated on successful login: the post-auth `/login` redirect (and
OAuth callback) clear the `iris_csrf` cookie so any pre-auth token capture becomes
useless. The next form render re-mints a fresh token via `attach_csrf_cookie`.

`POST /login` is rate-limited per client IP via an in-process token bucket (capacity
10, refill 0.2/sec — 10-attempt burst then ~12 attempts/minute sustained). Exhausted
clients receive 429 with `Retry-After`. Per-process state; multi-worker deploys would
need Redis.

## Identity matching

IdP identity maps to CH roles via two naming conventions:

- **Groups** — `<group>_GRP` in CH, where `<group>` is the verbatim IdP group string
  (exact, case-sensitive match against `User.groups`).
- **Users** — `<username>_USER` in CH, where `<username>` is sourced as:
  - OAuth: `preferred_username` claim, falling back to `sub` if absent.
  - LDAP: the `username` substituted into `LDAP_BIND_DN_TEMPLATE`.
  - Mock: `MOCK_USERNAME`.

Both role types are created lazily by `provision_user` on each login. They serve as
recipients of tier-role grants: `session.grant_writer("bob")` runs
`GRANT <X>_DBWRITER TO bob_USER`.

## Tests

`tests/conftest.py` sets `AUTH_METHOD=mock` (and the mock creds) at module scope via
`os.environ.setdefault` before iris is imported — this protects the suite from a
developer's OAuth/LDAP `.env`.

Available fixtures:

- `client` — unauthenticated `TestClient`. Use for tests that exercise the login
  flow, error pages, etc.
- `authed_client` — pre-creates a session in the SQLite `:memory:` store and attaches
  the cookie. Use for feature tests of routes that need "a logged-in user".

Provider tests are offline:

- LDAP: `ldap3.MOCK_SYNC` strategy with an in-memory directory
  (`tests/auth/test_provider_ldap.py`).
- OAuth: `httpx.MockTransport` mocking discovery / token / userinfo
  (`tests/auth/test_provider_oauth.py`).

## Integration tests

`tests/auth/integration/` runs the OAuth provider end-to-end against a real
`quay.io/keycloak/keycloak:26.0` container via `testcontainers-python`. Covers happy
paths and natural failure paths exercisable against a real IdP (wrong client secret,
code reuse, redirect_uri mismatch, wrong CA bundle) plus full TLS coverage.

```bash
# Run only the integration tier (requires Docker)
uv run pytest tests/auth/integration

# Skip the integration tier
uv run pytest --ignore=tests/auth/integration
```

Runtime: ~25s on a warm cache (Keycloak boot ~12s dominates). Session-scoped
containers amortize across the full integration suite.

The realm seed at `tests/auth/integration/seed/keycloak-realm.json` is committed and
declarative — it defines an `iris-test` realm with two users (`alice`/`secret` in
`admins`+`users`, `bob`/`hunter2` in `users`) and an `iris` client wired up with an
explicit `oidc-group-membership-mapper`. Without that mapper Keycloak doesn't emit a
`groups` claim, so users would land in iris with `groups=()`. TLS certs are generated
at session start via `_tls.py` and are not committed.

`OIDC_SCOPES` for integration tests is `openid profile email` (no `groups`). The
realm doesn't ship a `groups` client scope by default, but the client-level mapper
emits the claim regardless of requested scope.

## Module map

```
src/iris/auth/
├── __init__.py        # public surface: AuthSession, Capabilities, EMPTY_CAPABILITIES,
│                      #   Session, SessionOptional, SessionAdmin,
│                      #   SessionDatabaseCreator, SessionDatabaseAdmin,
│                      #   SessionWrite, SessionRead, User, install
├── rights.py          # Capabilities frozen dataclass + serialization helpers + EMPTY_CAPABILITIES
├── identity.py        # User (frozen+slots), StoredSession (mutable; internal store-row type)
├── views.py           # AuthSession + database-bound subclasses; CH-method delegation
├── config.py          # AuthSettings.from_env() — AUTH_METHOD, session TTLs, etc.
├── store.py           # SessionStore (SQLite): create / get_and_refresh /
│                      #   update_data / set_capabilities / delete / close
├── exceptions.py      # AuthRequired, AuthForbidden, AuthError +
│                      #   install_exception_handlers
├── deps.py            # the seven Annotated alias deps + set_session_store /
│                      #   set_settings
├── csrf.py            # double-submit CSRF: mint_csrf_token, attach_csrf_cookie,
│                      #   issue_csrf_token, verify_csrf_form, delete_csrf_cookie
├── rate_limit.py      # TokenBucket (used on POST /login)
├── routes.py          # /login, /login/callback, /logout, /api/whoami; install(app)
└── providers/         # mock, ldap, oauth — unchanged
    ├── __init__.py    # build_provider(settings) factory
    ├── base.py        # Provider Protocol
    ├── mock.py        # MockProvider
    ├── ldap.py        # LDAPProvider (ldap3 bind + group search)
    └── oauth.py       # OAuthProvider (OIDC discovery + PKCE + signed-cookie state)
```

The CH-side bootstrap (creating `iris_global_admin` + admin user/group roles from
`CLICKHOUSE_ADMIN_USER` / `CLICKHOUSE_ADMIN_GROUP`) lives in
`iris.clickhouse.bootstrap.bootstrap_admin`. It is called from `iris.clickhouse.install`
at app launch, not from anything in `iris.auth`.

`install(app)` reads env, builds the provider, and wires the auth router + exception
handlers + session store into a FastAPI app. Called from `build_app()` in
`src/iris/app.py`.

## Evolution

Dated design specs in `docs/superpowers/specs/`, oldest first:

- **2026-05-03** — initial auth scaffold + mock provider →
  `docs/superpowers/specs/2026-05-03-auth-design.md`
- **2026-05-03** — SQLite role-mapping subsystem (later removed) →
  `docs/superpowers/specs/2026-05-03-roles-authz-design.md`
- **2026-05-04** — session API simplification →
  `docs/superpowers/specs/2026-05-04-session-api-simplification-design.md`
- **2026-05-05** — auth integration tests via Keycloak testcontainer →
  `docs/superpowers/specs/2026-05-05-auth-testcontainers-design.md`
- **2026-05-06** — SQLite session store (replaces in-memory) →
  `docs/superpowers/specs/2026-05-06-sqlite-session-store-design.md`
- **2026-05-06** — authz moved to SQLite (later removed) →
  `docs/superpowers/specs/2026-05-06-authz-sqlite-design.md`
- **2026-05-08** — CH-only authorization (drops SQLite role mapping) →
  `docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md`
- **2026-05-08** — session-as-handle: one parameter per route →
  `docs/superpowers/specs/2026-05-08-session-as-handle-design.md`
