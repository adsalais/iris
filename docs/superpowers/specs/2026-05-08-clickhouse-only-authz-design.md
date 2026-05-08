# ClickHouse-only authorization

Replace iris's two-layer authorization model (SQLite-backed role mapping + ClickHouse RBAC) with a single source of truth: ClickHouse RBAC. Iris caches a derived view of CH grants on the session for fast route gating. There is no iris-managed authorization state outside CH — the SQLite database holds only sessions.

## Why

Today, two systems each hold authorization state:

- `iris.auth.authz` (SQLite tables `authz_*`): maps internal role names like `admin` and `clickhouse_admin` to IdP groups/usernames. Routes gate themselves with `Depends(require_role("admin"))`.
- `iris.clickhouse` (CH RBAC + SQLite tables `clickhouse_database_admins_*`): provisions per-user / per-group roles in CH and tracks per-database admin assignments in SQLite.

The two layers drift. Operators who add a user to a CH role through `clickhouse-client` get no iris-side recognition; operators who add an IdP-group → role mapping in iris see no CH effect until the user logs in again. The per-database admin tier was added on top of both layers, splitting "is X allowed?" across three places (IdP groups, iris role mapping, SQLite admin tables) plus CH itself.

This spec collapses the model: CH grants are the only source of truth. Iris derives a frozen `Rights` view from CH at login and caches it on the session. Mutations go through CH (via iris admin handles) and iris's view becomes accurate after the next login. The whole `iris.auth.authz` subpackage and the `clickhouse_database_admins_*` SQLite tables disappear.

## Scope

In:
- Delete `src/iris/auth/authz/` (mapping, store, bootstrap, core, deps).
- Delete `src/iris/clickhouse/database_admins.py` and the `clickhouse_database_admins_users` / `clickhouse_database_admins_roles` tables.
- Drop the `authz_*` tables (no migration — wipe and re-bootstrap is the documented upgrade path).
- Add `Rights` dataclass + derivation logic; cache on the session row.
- Rename the `Session` dataclass to `AuthSession`. Reintroduce `Session` / `SessionOptional` as `Annotated` alias deps. Add tier alias deps.
- Per-database tier roles (`X_DBADMIN`, `X_DBWRITER`, `X_DBREADER`): created at DB creation, dropped at DB deletion.
- Bootstrap (option β): on app boot, if `IRIS_BOOTSTRAP_USER` is set and no admin grant exists in CH, seed it.
- Test refactor: tier-role lifecycle tests, rights-derivation tests, end-to-end tier-promotion tests against the existing CH testcontainer.

Out:
- Sub-database granularity (table/column-level labels). CH still enforces these on the actual query; iris just doesn't model them at the route layer.
- Re-derivation of rights mid-session. Operator changes take effect on the user's next login.
- A migration tool for existing SQLite mappings. The deployment flow is "wipe `AUTH_DB_PATH`, set `IRIS_BOOTSTRAP_USER`, restart".
- Changes to the session lifetime model (sliding TTL, absolute cap, per-user cap).
- Connection pooling on the CH client.

## Decisions

### Granularity: database-level only

Labels fire only when a user has a grant covering an entire database (e.g., `GRANT SELECT ON finance.*`). A user with a sub-database grant (`GRANT SELECT ON finance.invoices`) gets no label and is rejected by the iris dep, even though CH would serve the query. Routes that require finer admission are not in scope.

### Right semantics

Five labels, with implied superset ordering. A session's effective answer to "does this session have read on X?" is `is_admin OR (X in db_admin) OR (X in db_writer) OR (X in db_reader)`.

| Label | CH grant pattern (a session gets this label iff its effective role set holds this grant) |
|---|---|
| `is_admin` (global) | `GRANT ALL ON *.* WITH GRANT OPTION` |
| `can_create_database` (global) | `GRANT CREATE DATABASE ON *.*` |
| `db_admin[X]` | `GRANT ALL ON X.* WITH GRANT OPTION` |
| `db_writer[X]` | All of `GRANT SELECT, INSERT, ALTER UPDATE ON X.*`, without `GRANT OPTION` |
| `db_reader[X]` | `GRANT SELECT ON X.*` |

`db_writer` is `SELECT + INSERT + ALTER UPDATE` (no DELETE; literal reading of "insert/update"). Adding DELETE later is non-breaking because the discriminator is "all of the listed privileges are granted", not "exactly these privileges".

`db_admin` is the only tier that requires `GRANT OPTION`. This is the iris-level marker for "may delegate"; routes that mutate grants (e.g., `add_writer`) gate on it.

### CH-side state: per-database tier roles

Per database `X`, iris maintains three named roles:

- `X_DBADMIN` — `GRANT ALL ON X.* WITH GRANT OPTION`
- `X_DBWRITER` — `GRANT SELECT, INSERT, ALTER UPDATE ON X.*`
- `X_DBREADER` — `GRANT SELECT ON X.*`

Per-tier grants on users / groups are expressed as role-grants:
- `add_writer(bob, on=finance)` → `GRANT finance_DBWRITER TO bob_USER`
- `add_admin(bob, on=finance)` → `GRANT finance_DBADMIN TO bob_USER`
- `add_writer_for_group(engineering, on=finance)` → `GRANT finance_DBWRITER TO engineering_GRP`

Revocation is symmetric (`REVOKE`).

The per-user `<username>_USER` and per-group `<group>_GRP` roles continue to exist exactly as today, created lazily by `init_user_rights` on each login. Their only purpose is to be the recipients of tier-role grants.

### Rights derivation

Called once per login, after `init_user_rights` succeeds. Inputs: the username and the list of group names from the IdP. Outputs: a `Rights` value persisted on the session row.

Procedure:

1. Resolve effective role names. The user's directly-granted roles are `<username>_USER` plus each `<group>_GRP`. Walk `system.role_grants` transitively to collect all roles reachable from this set; this gives the effective role set.
2. Match tier roles by suffix. For each role in the effective set whose name ends in `_DBADMIN`, `_DBWRITER`, or `_DBREADER`, split off the suffix to recover the database name and add to the appropriate `frozenset`.
3. Compute the global flags by querying `system.grants` filtered to the effective role set. CH always expands `GRANT ALL` into the underlying primitive privileges in `system.grants` — there is never a row with `access_type='ALL'`. Global-scope grants store `database`/`table`/`column` as `NULL`, not `''`. The implementation uses two markers:
   - `is_admin = True` if some role holds `ROLE ADMIN` at global scope (`database IS NULL`) with `grant_option=1`. ROLE ADMIN is part of the primitive expansion of `ALL` and is only granted to genuine admins (operators don't grant ROLE ADMIN selectively), so it's a reliable single-row marker.
   - `can_create_database = True` if some role holds `CREATE DATABASE` at global scope. Per spec this does not require GRANT OPTION.

Stored shape:

```python
@dataclass(frozen=True, slots=True)
class Rights:
    is_admin: bool
    can_create_database: bool
    db_admin: frozenset[str]
    db_writer: frozenset[str]
    db_reader: frozenset[str]

    def has_read(self, database: str) -> bool:
        return self.is_admin or database in (self.db_admin | self.db_writer | self.db_reader)
    def has_write(self, database: str) -> bool:
        return self.is_admin or database in (self.db_admin | self.db_writer)
    def has_admin(self, database: str) -> bool:
        return self.is_admin or database in self.db_admin
```

Persisted as JSON on the session row alongside the existing `data` column. The `frozenset`s serialize as sorted lists. On session refresh, `Rights` is rehydrated from the JSON, not re-derived from CH.

### Database name parsing

Database identifiers go through the existing `validate_identifier` helper (`[a-zA-Z0-9_]+`). Tier role names are `<database><suffix>` where suffix is one of `_DBADMIN`, `_DBWRITER`, `_DBREADER`. Suffix-anchored parsing (`name.endswith(suffix)`, then strip) is unambiguous because the suffixes are literal and the database part is constrained to the validated alphabet.

A database literally named `_DBADMIN` would parse as "empty database with `_DBADMIN` suffix"; the empty string fails `validate_identifier`, so it would be rejected at DB creation time. No special-case logic needed.

### Session shape

The dataclass renames from `Session` to `AuthSession` to free `Session` for the dep alias. The `roles` field is dropped: today it carried the closure-resolved internal role names (`admin`, `writer`, …), which no longer exist. Templates that want the user's IdP groups read `session.user.groups`; routes that want the authorization decision read `session.rights`.

```python
@dataclass(frozen=True, slots=True)
class AuthSession:
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    rights: Rights             # derived view used by route deps
```

### Dep aliases

Public surface:

```python
# the value type — what the deps return
AuthSession

# Annotated alias deps (consumed in routes)
Session                      # require auth; returns AuthSession; 401 on no-session
SessionOptional              # admit None; returns AuthSession | None
SessionAdmin                 # require rights.is_admin; 401/403
SessionDatabaseCreator       # require rights.is_admin or rights.can_create_database
SessionDatabaseAdmin         # require rights.has_admin(database); reads `database` from path
SessionWrite                 # require rights.has_write(database)
SessionRead                  # require rights.has_read(database)
```

The three database-scoped aliases resolve `database: str` from the route's path/query parameters via FastAPI's normal binding. Each underlying resolver function declares `database: str` as a regular parameter and `session: AuthSession = Depends(_require_session)` as the inner dep. FastAPI binds `database` from the calling route.

The previous `Annotated`-with-`Depends` conflict (which forced the temporary removal of the alias pattern) doesn't recur, because each policy now has its own dedicated alias — no caller writes `session: Session = Depends(<some other thing>)`. There is no `require_role(...)` factory in this model.

Failure shape:
- No session: 401, redirect to `/login` for HTML clients (existing behavior).
- Session but missing right: 403.
- No 500 case. Today's "role not configured" 500 disappears: rights are computed from CH at login, not looked up from a separate config table.

### Route examples

```python
@app.get("/")
async def home(session: SessionOptional):
    return templates.TemplateResponse("index.html", {"session": session})

@app.get("/me")
async def me(session: Session):
    return {"user": session.user.username, "rights": rights_to_dict(session.rights)}

@app.get("/clickhouse/databases/{database}/read")
async def read_db(database: str, session: SessionRead, handle: ClickHouseHandle = Depends(get_clickhouse_handle)):
    return await handle.query_as_user(f"SELECT count() FROM {quote_identifier(database)}.lines")

@app.post("/clickhouse/databases/{database}")
async def create_database(database: str, session: SessionDatabaseCreator,
                          handle: ClickHouseDatabaseCreatorHandle = Depends(get_creator_handle)):
    await handle.create_database(database)
    return {"created": database}

@app.post("/clickhouse/databases/{database}/grants/users/{username}")
async def grant_read(database: str, username: str, session: SessionDatabaseAdmin,
                     handle: ClickHouseDatabaseAdminHandle = Depends(get_admin_handle)):
    await handle.grant_reader(username)
    return {"granted": True}

@app.post("/clickhouse/databases/{database}/admins/users/{username}")
async def delegate_admin(database: str, username: str, session: SessionDatabaseAdmin,
                         handle: ClickHouseDatabaseAdminHandle = Depends(get_admin_handle)):
    await handle.add_admin_user(username)
    return {"ok": True}

@app.post("/admin/grant/database_creator/{username}")
async def grant_database_create(username: str, session: SessionAdmin,
                                handle: ClickHouseAdminHandle = Depends(require_clickhouse_admin)):
    await handle.grant_database_creation(username)
    return {"ok": True}
```

### Lifecycle: tier-role create/drop

`create_database(name)` is a single logical step (not transactional in the SQL sense, since CH DDL doesn't transact, but ordered to be safely re-runnable):

1. `CREATE DATABASE IF NOT EXISTS <name>`
2. `CREATE ROLE IF NOT EXISTS <name>_DBADMIN`
3. `CREATE ROLE IF NOT EXISTS <name>_DBWRITER`
4. `CREATE ROLE IF NOT EXISTS <name>_DBREADER`
5. `GRANT ALL ON <name>.* TO <name>_DBADMIN WITH GRANT OPTION`
6. `GRANT SELECT, INSERT, ALTER UPDATE ON <name>.* TO <name>_DBWRITER`
7. `GRANT SELECT ON <name>.* TO <name>_DBREADER`
8. `GRANT <name>_DBADMIN TO <creator>_USER`

All steps are idempotent. Re-running the operation against an existing database is a no-op for steps 1-7 and adds the creator to admins again (which CH treats as no-op).

`delete_database(name)` is new. Gated on `SessionDatabaseAdmin`:

1. `DROP DATABASE IF EXISTS <name>`
2. `DROP ROLE IF EXISTS <name>_DBADMIN`
3. `DROP ROLE IF EXISTS <name>_DBWRITER`
4. `DROP ROLE IF EXISTS <name>_DBREADER`

Without step 2-4, dropped-database tier roles would orphan in CH and reappear in the rights derivation as labels for non-existent databases. Drop order: data first, then roles, so a failure between steps doesn't leave a database with no admin role.

### Username enumeration: pre-create on grant

The current security property — admins granting permissions to a user who hasn't logged in yet should not learn whether the user exists in the IdP — is preserved. Mutator helpers `grant_reader(username)` / `grant_writer(username)` / `add_admin_user(username)` first issue `CREATE ROLE IF NOT EXISTS <username>_USER` before issuing the role-grant. The role exists in CH whether or not the user has ever authenticated; `init_user_rights` is idempotent on the existing role at first login.

### Row policies

Row policies attach to tier roles, not per-user roles. The natural call site is `add_row_policy(database, table, column, "<database>_DBREADER", value)` to scope all readers of a database. The CH-side surface (the helpers in `iris.clickhouse.policies`) is unchanged — only the role names callers pass shift.

The wildcard service-admin policy (`USING 1` for `iris_service`) continues to be managed exactly as today: `add_row_policy` ensures it on first call per table, `revoke_row_policy` does not drop it.

### Bootstrap (option β)

At app boot, after `ensure_service_admin`:

1. If `IRIS_BOOTSTRAP_USER` is unset: skip.
2. Else, query CH: does any role hold `ALL ON *.* WITH GRANT OPTION`? If yes: skip (idempotent).
3. Else: `CREATE ROLE IF NOT EXISTS <bootstrap>_USER`, then `GRANT ALL ON *.* WITH GRANT OPTION TO <bootstrap>_USER`.

The bootstrap role is the per-user role for the configured username. When that user logs in, `init_user_rights` is idempotent on the existing role and the rights derivation finds `is_admin=True`. Wiping CH and restarting re-triggers the bootstrap. Operators who want to re-bootstrap manually: drop the user role, restart.

The `clickhouse_admin` separate role (today's seeded include) disappears. There is just `is_admin`, computed from a single CH grant pattern. The `iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE` constant and the `tests/auth/authz/test_authz_bootstrap.py` drift test go with it.

### Configuration

Environment variables that go away:
- `AUTHZ_BOOTSTRAP_ROLE` — there's no separate role registry to seed.
- `AUTHZ_BOOTSTRAP_USER` — replaced by `IRIS_BOOTSTRAP_USER` (the `AUTHZ_` prefix referred to a subsystem that no longer exists; renaming is cheap given the big-bang migration).

Environment variables added:
- `IRIS_BOOTSTRAP_USER` — username seeded as admin in CH on first install.

Environment variables that stay:
- All `AUTH_*`, `OIDC_*`, `LDAP_*`, `MOCK_*`, `CLICKHOUSE_*`, `SESSION_*` variables.

`CLICKHOUSE_SERVICE_ADMIN_ROLE` continues to exist (it's the service identity that actually runs DDL), independent of any user-facing tier.

## Module map (post-change)

```
src/iris/auth/
├── __init__.py              # AuthSession, Rights, Session, SessionOptional, SessionAdmin, SessionDatabaseCreator, SessionDatabaseAdmin, SessionWrite, SessionRead, User, install
├── session.py               # AuthSession dataclass (renamed from Session) + Rights dataclass
├── config.py                # AuthSettings.from_env() — sub-settings unchanged
├── identity.py              # User, UserSession (internal mutable session row)
├── sessions.py              # SessionStore — schema gains a rights JSON column
├── exceptions.py            # AuthRequired, AuthForbidden, AuthError, install_exception_handlers (drop AuthorizationMisconfigured)
├── deps.py                  # ALL alias deps (Session, SessionOptional, SessionAdmin, SessionDatabaseCreator, SessionDatabaseAdmin, SessionWrite, SessionRead) + their resolver functions
├── csrf.py                  # unchanged
├── rate_limit.py            # unchanged
├── routes.py                # /login, /login/callback, /logout, /api/whoami; install(app)
├── bootstrap.py             # NEW: bootstrap option β at app boot
└── providers/               # mock, ldap, oauth — unchanged
```

```
src/iris/clickhouse/
├── __init__.py              # public surface (handles + bridge deps; no tier aliases here)
├── audit.py                 # gains tier-membership listing helpers
├── bootstrap.py             # ensure_service_admin (unchanged)
├── client.py                # unchanged
├── config.py                # unchanged
├── deps.py                  # get_clickhouse_handle, require_clickhouse_admin, require_clickhouse_database_admin, require_clickhouse_database_creator (handle providers only)
├── grants.py                # tier-aware mutators (grant_reader, grant_writer, add_admin_user, etc.)
├── handle.py                # ClickHouseHandle, ClickHouseAdminHandle, ClickHouseDatabaseAdminHandle, ClickHouseDatabaseCreatorHandle
├── identifiers.py           # unchanged
├── install.py               # wires post-login hook, skips DatabaseAdminStore (deleted)
├── policies.py              # unchanged surface; callers pass tier role names
├── rights.py                # NEW: derive_rights(client, username, groups) -> Rights
└── users.py                 # init_user_rights (unchanged contract; called from post-login hook)
```

Deleted: `src/iris/auth/authz/` (whole subpackage) and `src/iris/clickhouse/database_admins.py`.

## Where the tier-alias deps live

All alias deps live in `iris.auth.deps`. None of them need to import from `iris.clickhouse`: each one only inspects `session.rights.<flag>` or calls `session.rights.has_*(database)`. The `Rights` value type lives next to `AuthSession` in `iris.auth.session`.

The CH-side derivation function (`derive_rights`) lives in `iris.clickhouse.rights` because computing `Rights` requires hitting CH's `system.role_grants` and `system.grants`. The post-login hook in `iris.clickhouse.install` calls `derive_rights` after `init_user_rights` and stuffs the result into the new session row. This keeps the dep-module dependency graph one-way: `iris.clickhouse → iris.auth`, never the reverse.

Route authors get a single import for the dep aliases (`from iris.auth import Session, SessionRead`) and a separate import for the CH-flavored handle providers (`from iris.clickhouse import get_clickhouse_handle, require_clickhouse_admin`). Routes that need both write two imports; the ergonomics are no worse than today.

## Tests

- `tests/auth/authz/` — deleted with the module.
- `tests/auth/integration/` (Keycloak): existing assertions about `session.roles` driving authorization (the field is dropped) switch to `session.rights`. The tier promotions ("admin user grants reader to bob, bob logs in, sees finance.lines") gain end-to-end coverage against both Keycloak and the CH testcontainer.
- `tests/clickhouse/`:
  - tier-role lifecycle tests (`create_database` produces all three roles + grants + member, `delete_database` cleans up all four objects, idempotency under repeat).
  - rights-derivation tests (matrix: user has direct grant / group grant / transitive grant / wildcard grant → expected `Rights` shape).
  - tier-promotion end-to-end (creator creates DB, grants writer to bob, bob logs in, bob's writes succeed via `query_as_user`, bob's attempt to delegate is rejected by the dep gate).
  - row-policy attachment to tier roles (operator-as-admin attaches policy to `finance_DBREADER`; reader-of-finance bob sees only filtered rows).
- `tests/auth/test_session_rename.py` — single-shot test that `from iris.auth import AuthSession, Session, SessionOptional` works, AuthSession is a frozen dataclass, and Session/SessionOptional are Annotated aliases.

The mock provider continues to drive non-CH unit tests of session shape; rights are populated by hitting the CH testcontainer for any test that needs a non-empty `Rights`. Tests that don't need CH at all (login flow tests, CSRF tests) construct `AuthSession` with `Rights(is_admin=False, can_create_database=False, db_admin=frozenset(), db_writer=frozenset(), db_reader=frozenset())`.

## Open risks

- **Derivation latency at login:** today's role-mapping store is one in-process SQLite query; the new derivation makes a small number of CH queries (role-grants walk + grants enumeration). Login pays an extra CH round-trip group. Acceptable at ≤20-user scale; profile and consider caching if scaling.
- **Grant enumeration scope:** the rights derivation queries `system.role_grants` and `system.grants` filtered to the user's effective role set. CH service-account permissions to read these views are part of `ensure_service_admin` (which already grants ALL, so this is satisfied implicitly).
- **Tier-role suffix collision with operator-created roles:** if an operator manually creates a CH role named `something_DBADMIN` outside iris's lifecycle, iris will treat it as a tier role. This is by design — iris reads CH as the source of truth. Document.
- **Rights cached on session, not invalidated on grant change:** an operator who revokes Bob's writer access mid-session has Bob keep writing until his next login. Acceptable (matches today's role-mapping freshness model). Operators wanting hard cutoff: revoke at CH (so the actual query fails) AND delete the user's session rows.

## Migration / rollout

Big-bang. The deployment runbook for the upgrade:

1. Stop iris.
2. Operator records the current authz mapping (groups → roles, users → roles, includes) and per-DB admins out-of-band.
3. Wipe `AUTH_DB_PATH`. This drops the old authz tables AND all active sessions — every user is forced back through login. Stopping iris first means no in-flight requests are killed.
4. Replace `AUTHZ_BOOTSTRAP_USER` with `IRIS_BOOTSTRAP_USER` in the deployment env, set to the operator's username.
5. Start iris. The bootstrap seeds admin in CH.
6. Operator logs in, then through iris admin routes (or directly in CH) re-creates the per-DB tier grants matching the recorded mapping.

There is no in-place migration tool. The justification: the model is small (≤20 users, ≤dozens of DBs), the mapping is human-recordable, and an automated migration would carry its own bugs and add code we'd delete on the next release.
