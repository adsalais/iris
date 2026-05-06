# Auth ↔ ClickHouse bridge — design

**Date:** 2026-05-06
**Status:** draft, pending review

## Problem

`iris.auth` and `iris.clickhouse` were built independently. The auth package provisions sessions and resolves internal roles from YAML; the clickhouse package can provision users, roles, grants, row policies, and run audit queries. Nothing currently links the two. A logged-in user has no way to issue an impersonated ClickHouse query, and there is no FastAPI dependency that hands a route the right query handle.

We want routes to declare their ClickHouse posture at the type level:

```python
@app.get("/click-user")
async def user_route(handle: ClickHouseHandle = Depends(get_clickhouse_handle)): ...

@app.get("/click-admin")
async def admin_route(handle: ClickHouseAdminHandle = Depends(require_clickhouse_admin)): ...
```

A non-admin must not be able to run unimpersonated queries or administrative functions, and that prohibition should be enforced both at request time (403 from the dep) and at compile time (basedpyright sees no admin methods on the user handle).

## Non-goals

- Connection pooling and multi-worker session sharing remain v1.1 deferred concerns.
- A general-purpose `execute_as(username, sql)` helper outside the request/response cycle is not part of this work.
- No changes to the YAML schema. `clickhouse_admin` is modeled as a regular role; operators map it to whatever IdP groups they want.

## Architecture

### Two handle types

```
ClickHouseHandle                   ← any logged-in user
└── query_as_user(sql, params)     impersonates the session's user via EXECUTE AS

ClickHouseAdminHandle(ClickHouseHandle)   ← gated on clickhouse_admin role
├── query_as_service(sql, params)         no impersonation; runs as iris_service
├── reprovision_user(username, groups)    delegates to init_user_rights
├── grant_select_to_database(role, database)
├── grant_insert_update_to_table(role, database, table)
├── add_row_policy(*, database, table, column, role, value)
├── revoke_row_policy(*, database, table, column, role, value)
├── user_grants(username)
├── role_grants(role)
├── user_role_memberships(username)
├── user_row_policies(username)
├── role_row_policies(role)
└── table_row_policies(database, table)
```

The admin handle inherits from the user handle, so admin routes that want to issue an impersonated query call `handle.query_as_user(...)` on their `ClickHouseAdminHandle`.

A non-admin route declares `handle: ClickHouseHandle`. The class has no admin methods, so basedpyright rejects any call to `query_as_service`, `grant_*`, etc. The role check at the dep boundary catches the runtime case; the type system catches the developer-error case.

Both handles are async-method even though `clickhouse-connect` is sync. Each method wraps the underlying driver call in `asyncio.to_thread` to keep slow CH queries from blocking the FastAPI event loop.

### Impersonation mechanism

ClickHouse provides `EXECUTE AS <user>` SQL syntax. Two forms exist:

1. `EXECUTE AS user;` — sets impersonation for the entire session/connection.
2. `EXECUTE AS user; <query>` — single-query impersonation; identity reverts after.

The handle uses **form 2 only**. Form 1 would leak identity across requests when the shared app-scoped client serves concurrent users. Username interpolation goes through the existing `iris.clickhouse.identifiers.quote_identifier(username, kind="username")` to keep the DDL-safety contract intact.

`query_as_service` does not prefix `EXECUTE AS` — it runs as the service-admin connection identity directly.

Implementation note (verified at plan time): whether `clickhouse-connect`'s `client.query()` accepts `EXECUTE AS x; SELECT …` as one HTTP round-trip, or whether a different driver call (`raw_query`, multi-statement command) is required. The mechanism is committed at design level; the exact call shape is a plan-stage concern.

### Two dependencies

`src/iris/clickhouse/deps.py` (new):

```python
CLICKHOUSE_ADMIN_ROLE: Final = "clickhouse_admin"

async def get_clickhouse_handle(
    request: Request, session: Session
) -> ClickHouseHandle:
    return ClickHouseHandle(
        client=request.app.state.clickhouse_client,
        username=session.user.username,
    )

async def require_clickhouse_admin(
    request: Request, session: Session, mapping: CurrentMapping
) -> ClickHouseAdminHandle:
    if CLICKHOUSE_ADMIN_ROLE not in mapping.roles:
        raise AuthorizationMisconfigured(CLICKHOUSE_ADMIN_ROLE)
    if CLICKHOUSE_ADMIN_ROLE not in session.roles:
        raise AuthForbidden(
            needed=(CLICKHOUSE_ADMIN_ROLE,),
            have=tuple(sorted(session.roles)),
        )
    return ClickHouseAdminHandle(
        client=request.app.state.clickhouse_client,
        username=session.user.username,
        settings=request.app.state.clickhouse_settings,
    )
```

The role-gate logic mirrors `iris.auth.authz.deps.require_role` so failure modes (`AuthForbidden` → 403, `AuthorizationMisconfigured` → 500) are consistent with the rest of the auth surface.

The `clickhouse_admin` string is bound once as a module constant. Operators must define a role with that exact name in `authz.yaml`; if they do not, the first request to a `require_clickhouse_admin`-gated route 500s with the role name logged. This matches the existing fail-loud posture of `require_role`.

### Module layout

```
src/iris/clickhouse/
├── __init__.py        re-exports the new public surface
├── handle.py          NEW   ClickHouseHandle, ClickHouseAdminHandle (no auth import)
├── deps.py            NEW   get_clickhouse_handle, require_clickhouse_admin, CLICKHOUSE_ADMIN_ROLE
├── install.py         NEW   install(app)
└── ... existing files unchanged
```

`handle.py` takes plain-data inputs (a `Client`, a `username`, a `ClickHouseSettings`). It does not import from `iris.auth`. The package's "independent of iris.auth" property is preserved at the level of the data model; only `deps.py` (FastAPI bridge) imports from auth, mirroring how `iris.auth.routes` imports FastAPI but `iris.auth.identity` does not.

```
src/iris/auth/
├── routes.py          MODIFIED   _finalize_login_redirect runs post-login hooks
└── deps.py            MODIFIED   small helper for hook registration (optional)

src/iris/app.py        MODIFIED   build_app calls iris.clickhouse.install(app) after iris.auth.install(app)
```

### Login hook & lifecycle

`init_user_rights` runs **once per real authentication**. The seam is `_finalize_login_redirect` in `src/iris/auth/routes.py:62-82`:

```python
session = await store.create(user)
for hook in getattr(app.state, "post_login_hooks", ()):
    await hook(user)             # propagates exceptions — fail-loud
logger.info(...)
```

Auth defines a generic, ordered list of post-login hooks on `app.state`. It does not import `iris.clickhouse`.

`iris.clickhouse.install(app)` appends a `_provision_on_login` hook:

```python
def install(app: FastAPI) -> None:
    settings = ClickHouseSettings.from_env()
    client = build_client(settings)
    ensure_service_admin(client, settings)         # idempotent, fail-loud at boot
    app.state.clickhouse_client = client
    app.state.clickhouse_settings = settings

    async def _provision_on_login(user: User) -> None:
        await asyncio.to_thread(
            init_user_rights,
            client,
            username=user.username,
            groups=list(user.groups),
            settings=settings,
        )

    app.state.post_login_hooks = [
        *getattr(app.state, "post_login_hooks", ()),
        _provision_on_login,
    ]
```

`build_app()` in `src/iris/app.py` calls `iris.auth.install(app)` first, then `iris.clickhouse.install(app)` — order matters because clickhouse appends to a list that auth establishes (or that defaults to empty if auth hasn't been called).

**Lifecycle properties:**

- The hook fires on form-login submit success and OAuth callback success. Cookie-based session refresh on subsequent requests does *not* trigger the hook — sliding-TTL refresh bypasses `_finalize_login_redirect`.
- Group changes between two logins are reconciled by `init_user_rights` itself (it revokes stale `_GRP` memberships and grants new ones; see `src/iris/clickhouse/users.py:52-59`).
- If CH is unreachable at boot, `ensure_service_admin` raises and the app refuses to start.
- If CH is unreachable at login time, the hook raises and login returns 500. The user is told to retry.
- Sync `init_user_rights` is wrapped in `asyncio.to_thread` so a slow provisioning does not block the event loop.

### Configuration

No new env vars. The existing `CLICKHOUSE_*` set defined in `src/iris/clickhouse/config.py` covers everything needed for the shared client. The `clickhouse_admin` role name is a `Final` Python constant in `iris.clickhouse.deps`; making it env-configurable would break symmetry with the rest of the auth system, where internal role names are fixed contracts and only IdP-group mappings are operator-configurable.

### Error handling

| Failure | HTTP status | Source |
|---|---|---|
| No session | 401 | existing `Session` dep |
| `clickhouse_admin` not in user's effective roles | 403 (`AuthForbidden`) | `require_clickhouse_admin` |
| `clickhouse_admin` not defined in YAML | 500 (`AuthorizationMisconfigured`) | `require_clickhouse_admin` |
| `clickhouse-connect` driver error in `query_as_user` / `query_as_service` | 500 (propagated unless caught) | route handler |
| `init_user_rights` raises during login | 500 (fail-loud) | login route |
| `ensure_service_admin` raises at boot | App refuses to start | `iris.clickhouse.install` |
| Bad identifier (username, role, db, table) | `ValueError` from `validate_identifier` | unchanged |

## Testing

Three layers, all reuse existing fixtures.

### Unit tests (mocked Client)

Under `tests/clickhouse/test_handle.py`:

- `query_as_user` prepends `EXECUTE AS <quoted_username>` to the SQL submitted to the mocked `Client`.
- `ClickHouseAdminHandle` exposes `query_as_service`, `reprovision_user`, the `grant_*` and policy methods, and the audit functions.
- `query_as_service` does NOT contain `EXECUTE AS` in the submitted SQL.
- The user handle never receives an admin-method call (basedpyright catches this in code, but a unit test confirms the class hierarchy is what it claims).

Under `tests/clickhouse/test_deps.py`:

- `get_clickhouse_handle` raises 401 when no session exists.
- `require_clickhouse_admin` raises 500 (`AuthorizationMisconfigured`) when `clickhouse_admin` is missing from the YAML.
- `require_clickhouse_admin` raises 403 (`AuthForbidden`) when the user lacks the role.
- `require_clickhouse_admin` returns a `ClickHouseAdminHandle` for an admin user.

### Integration tests (testcontainer)

Under `tests/clickhouse/test_handle_integration.py`, reuse the session-scoped Keycloak-free CH container in `tests/clickhouse/conftest.py`:

- End-to-end: `EXECUTE AS alice; SELECT user()` returns `alice`.
- End-to-end: `query_as_service` returns the service-admin user.
- A user without `IMPERSONATE` granted on themselves cannot be impersonated (sanity check that CH enforces).

### Bridge tests (auth + CH)

Under `tests/clickhouse/test_login_provisioning.py`:

- Build a real `app` (calling both `iris.auth.install` and `iris.clickhouse.install`).
- Drive a form-login through `TestClient`.
- Assert that the CH user `alice` exists with the expected `_GRP` memberships.
- Drive a second login with different groups; assert the membership is reconciled.
- Drive a login while CH is unreachable; assert the response is 500 and no session was created.

Auth tests under `tests/auth/` are untouched. They call `iris.auth.install(app)` only, leaving `app.state.post_login_hooks` empty — the hook loop is a no-op for them.

## Public surface

After this work, `from iris.clickhouse import …` exposes:

- Existing: `ClickHouseSettings`, `build_client`, `ensure_service_admin`, `init_user_rights`, `grant_*`, `add_row_policy`, `revoke_row_policy`, the audit helpers.
- New: `ClickHouseHandle`, `ClickHouseAdminHandle`, `get_clickhouse_handle`, `require_clickhouse_admin`, `install`, `CLICKHOUSE_ADMIN_ROLE`.

## Open follow-ups (not in this spec)

- Connection pooling and multi-worker session sharing — same v1.1 deferred concern as `InMemorySessionStore`.
- A streaming variant of `query_as_user` for routes that need to stream large result sets back through Datastar SSE.
- Bridge test fixtures might be promoted to a shared `tests/conftest.py` if other test tiers need them; deferred until a second consumer appears.
