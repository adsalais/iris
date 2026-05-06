# Authz mapping in SQLite — design

**Date:** 2026-05-06
**Status:** draft, pending review

## Problem

The role mapping (which IdP groups and usernames satisfy which internal roles) lives in `authz.yaml`, loaded by `RoleMappingLoader` and re-read on mtime change. Editing requires file system access; programmatic mutation isn't possible; bootstrap is "edit a file before deploy."

We want the mapping in SQLite alongside the session store, with a programmatic CRUD API that future admin routes call. To avoid the chicken-and-egg problem of an empty mapping on a fresh deploy, two env vars seed a single bootstrap admin user on first install.

## Non-goals

- Coexistence with `authz.yaml`. Big-bang cutover: the YAML loader, the `AUTHZ_CONFIG_PATH` env var, and the `pyyaml` runtime dep all go away.
- HTTP routes that expose the mutator API. Future work; this spec only ships the Python surface.
- A separate auth DB file. The SQLite file backing sessions is reused for authz.

## Architecture

`iris.auth.authz` gains a `RoleMappingStore` class that replaces `RoleMappingLoader`. The store opens its own `sqlite3.Connection` against the same file as `SessionStore` (one connection per concern, both targeting one file — WAL handles coexistence). PRAGMAs match `SessionStore`'s setup (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`); these are connection-level (`synchronous`, `busy_timeout`) or file-level idempotent (`journal_mode`), so re-applying them in the second connection is safe. All sync sqlite3 calls are wrapped in `asyncio.to_thread`.

The existing `current_mapping` dep stays the same shape — `Annotated[RoleMapping, Depends(...)]` — but its underlying resolver fetches from `RoleMappingStore.get_mapping()` rather than from the loader. Routes and other deps see no change.

```
src/iris/auth/authz/
├── __init__.py
├── core.py         # resolve_roles, current_mapping (now reads from app.state.authz_store)
├── deps.py         # require_role (unchanged)
├── mapping.py      # RoleMapping, RoleDef, _compute_closure (parse() + YAML loader removed)
├── store.py   NEW  # RoleMappingStore: get_mapping + 8 mutators + bootstrap helper
└── bootstrap.py NEW # install_authz_schema(): first-install detection + seeding
```

`RoleMappingLoader` (loader.py) and `AuthzSettings` (config.py) are deleted.

## DB rename

`SESSION_DB_PATH` → `AUTH_DB_PATH`. Default `./iris-auth.db`. The single auth DB file holds both `sessions` and `authz_*` tables.

`AuthSettings.from_env()` reads `AUTH_DB_PATH` instead of `SESSION_DB_PATH`. No fallback — the rename happens once. Tests' `tests/conftest.py` switches its `os.environ.setdefault("SESSION_DB_PATH", ":memory:")` to `os.environ.setdefault("AUTH_DB_PATH", ":memory:")`.

## Schema

Four tables. Foreign keys provide the integrity that the YAML parser used to enforce by hand.

```sql
-- One row per defined role. Name is the regex [a-zA-Z0-9_-]+ (validated app-side).
CREATE TABLE IF NOT EXISTS authz_roles (
    name TEXT PRIMARY KEY
);

-- IdP group → role assignments (a role lists groups whose members get it).
CREATE TABLE IF NOT EXISTS authz_role_groups (
    role_name  TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (role_name, group_name),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

-- Username → role assignments. Stored lowercased (case-insensitive match).
CREATE TABLE IF NOT EXISTS authz_role_users (
    role_name      TEXT NOT NULL,
    username_lower TEXT NOT NULL,
    PRIMARY KEY (role_name, username_lower),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

-- Role inheritance edges (DAG; cycles rejected app-side on insert).
CREATE TABLE IF NOT EXISTS authz_role_includes (
    role_name     TEXT NOT NULL,
    included_role TEXT NOT NULL,
    PRIMARY KEY (role_name, included_role),
    FOREIGN KEY (role_name)     REFERENCES authz_roles(name) ON DELETE CASCADE,
    FOREIGN KEY (included_role) REFERENCES authz_roles(name) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_authz_role_groups_group ON authz_role_groups(group_name);
CREATE INDEX IF NOT EXISTS idx_authz_role_users_user   ON authz_role_users(username_lower);
CREATE INDEX IF NOT EXISTS idx_authz_role_includes_inc ON authz_role_includes(included_role);
```

Notes:
- `authz_roles` carries no metadata. If `description` or `created_at` are needed later, ALTER TABLE.
- `ON DELETE CASCADE` on the child tables means dropping a role is one statement; child rows disappear with it.
- `ON DELETE RESTRICT` on `included_role` prevents deleting a role that another role still includes — same behavior the YAML's `includes` validation enforced.
- Cycles are detected app-side (SQLite can't enforce graph acyclicity). The cycle check on `add_include` walks the closure that would result from adding the edge; rejects if it forms a cycle. Same algorithm as the existing `mapping.py:_compute_closure` cycle detection.

## Bootstrap

Two env vars on first install only:

```
AUTHZ_BOOTSTRAP_ROLE=admin       # default: "admin"
AUTHZ_BOOTSTRAP_USER=alice       # if unset, no bootstrap
```

`bootstrap.py:install_authz_schema(conn, settings)` runs at `iris.auth.install(app)` time:

```python
table_exists = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authz_roles'"
).fetchone() is not None

conn.executescript(_AUTHZ_SCHEMA)   # idempotent: CREATE IF NOT EXISTS

if table_exists:
    return                          # existing install — operator owns config

if not settings.bootstrap_user:
    return                          # fresh DB, operator chose not to seed

role = settings.bootstrap_role
# Hardcoded — must match iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE.
ch_admin = "clickhouse_admin"

conn.execute("INSERT INTO authz_roles(name) VALUES (?)", (role,))
conn.execute("INSERT INTO authz_roles(name) VALUES (?)", (ch_admin,))
conn.execute(
    "INSERT INTO authz_role_includes(role_name, included_role) VALUES (?, ?)",
    (role, ch_admin),
)
conn.execute(
    "INSERT INTO authz_role_users(role_name, username_lower) VALUES (?, ?)",
    (role, settings.bootstrap_user.lower()),
)
```

**Properties:**
- Bootstrap fires once: when `authz_roles` doesn't exist before this call. Schema creation always runs (idempotent), but seeding only happens on first install.
- Subsequent boots: tables exist, function returns early. Env var changes after the first boot have no effect.
- Operators can rename/delete the bootstrap role, change includes, remove the bootstrap user — the next boot won't fight them.
- Wiping the DB file (or pointing `AUTH_DB_PATH` at a new file) re-triggers bootstrap.
- If `AUTHZ_BOOTSTRAP_USER` is unset on first install, the DB has empty tables and no admin. Role-gated routes 500 until the operator populates the mapping via the mutator API. (Documented; this is the operator's choice.)
- The bootstrap admin includes `clickhouse_admin` so the seeded user immediately has CH admin powers. If `iris.clickhouse.install()` isn't called (e.g., `IRIS_NO_CLICKHOUSE=1`), the empty `clickhouse_admin` role is harmless — nothing references it.

The `clickhouse_admin` string is the only place auth mentions a CH-specific concept. A comment in `bootstrap.py` flags the coupling with the constant in `iris.clickhouse.deps`. The bootstrap test asserts the exact string is in the include edge so any drift surfaces immediately.

## RoleMappingStore API

```python
class RoleMappingStore:
    def __init__(self, *, path: str) -> None: ...

    async def get_mapping(self) -> RoleMapping: ...

    # Mutators — single SQL statement each (plus app-side validation).
    async def add_role(self, name: str) -> None: ...
    async def remove_role(self, name: str) -> None: ...

    async def add_group_to_role(self, role: str, group: str) -> None: ...
    async def remove_group_from_role(self, role: str, group: str) -> None: ...

    async def add_user_to_role(self, role: str, username: str) -> None: ...
    async def remove_user_from_role(self, role: str, username: str) -> None: ...

    async def add_include(self, role: str, included_role: str) -> None: ...
    async def remove_include(self, role: str, included_role: str) -> None: ...

    async def close(self) -> None: ...
```

**`get_mapping()`:** four `SELECT` queries (one per table), assembled into `dict[str, RoleDef]`, then `_compute_closure()` produces the closure dict. Returns the existing `RoleMapping` value type. Per-request — no in-memory cache, since at ≤20 users sub-millisecond reads stay sub-millisecond.

**Mutators:** validation matches the existing YAML loader's rules — role names must match `[a-zA-Z0-9_-]+` (the existing `mapping.py:_ROLE_NAME_RE`); group names and usernames are accepted as-is (same as today, where the YAML simply stored whatever the operator wrote). Usernames are lowercased for storage. On a regex violation, mutators raise `RoleMappingError` with the same message style as the existing parser. The `iris.clickhouse.identifiers` helper isn't reused — that's stricter (`[a-zA-Z0-9_]+`) and tied to DDL safety; authz strings don't reach DDL.

`add_role`, `add_group_to_role`, `add_user_to_role`: `INSERT OR IGNORE` — already-present rows are silently no-ops. Use `INSERT OR IGNORE` because the natural admin-UI use case is "ensure this assignment exists" rather than "fail if duplicate."

`remove_role`: `DELETE FROM authz_roles WHERE name = ?`. `ON DELETE CASCADE` on the child tables cleans groups/users/includes that referenced this role. The FK RESTRICT on `included_role` raises `IntegrityError` if any *other* role still includes this one — that's caught and re-raised as `RoleMappingError(f"role {name!r} is included by other roles")`.

`add_include`: must run a cycle check before inserting. The check fetches the existing includes table, virtually adds `(role, included_role)`, runs DFS from `role` to see if it can reach itself. If yes, raises `RoleMappingError(f"cycle detected: {role} -> ... -> {role}")`. Insert otherwise.

All sync sqlite3 calls wrapped in `asyncio.to_thread`. The store has its own `asyncio.Lock` for write coordination within a process.

## Wiring

`iris.auth.routes.install`:

```python
from iris.auth.authz.bootstrap import install_authz_schema
from iris.auth.authz.store import RoleMappingStore

# ... existing settings/store/provider code ...

# After SessionStore is built and stashed on app.state:
store = SessionStore(path=settings.auth_db_path, ...)
app.state.auth_session_store = store
app.state.auth_close_session_store = store.close

# Authz: separate store, separate connection on the same file.
mapping_store = RoleMappingStore(path=settings.auth_db_path)
# install_authz_schema runs on the *internal* connection of mapping_store.
# It reads AUTHZ_BOOTSTRAP_ROLE / AUTHZ_BOOTSTRAP_USER off settings.
mapping_store.bootstrap(settings)
app.state.authz_store = mapping_store
app.state.auth_close_authz_store = mapping_store.close
```

`iris.auth.authz.core.current_mapping(request)` changes from:

```python
return request.app.state.authz_loader.get()
```

to:

```python
return await request.app.state.authz_store.get_mapping()
```

`current_mapping` was sync; switching to async is a small ripple — the `Annotated` `CurrentMapping` already uses `Depends(current_mapping)` and FastAPI handles sync/async deps interchangeably.

`_lifespan` in `src/iris/app.py` adds the new closer alongside the session-store one:

```python
authz_closer = getattr(app.state, "auth_close_authz_store", None)
if authz_closer is not None:
    await authz_closer()
```

## Configuration changes

**Removed env vars:**
- `AUTHZ_CONFIG_PATH` — file no longer read.
- `SESSION_DB_PATH` — renamed to `AUTH_DB_PATH`.

**New env vars:**
- `AUTH_DB_PATH` — replaces `SESSION_DB_PATH`. Default `./iris-auth.db`.
- `AUTHZ_BOOTSTRAP_ROLE` — default `admin`.
- `AUTHZ_BOOTSTRAP_USER` — optional; if set, seeds an admin user on first install.

`AuthSettings` gains `auth_db_path: str`, `bootstrap_role: str`, `bootstrap_user: str | None`. Drops `session_db_path`. The existing `AuthzSettings` class is deleted (no more YAML path).

## Migration / cleanup

Big-bang. After this work:

- Files deleted: `src/iris/auth/authz/loader.py`, `src/iris/auth/authz/config.py`.
- Functions deleted: `mapping.py:parse()`, the `_NoDuplicatesSafeLoader` YAML loader subclass, `_coerce_string_list`. The exception class `RoleMappingError` and the value types `RoleDef` / `RoleMapping` / `_compute_closure` stay.
- Tests deleted: `tests/auth/authz/test_role_mapping.py` (YAML parser tests), `tests/auth/test_authz_loader.py` (loader tests). Whatever's still useful from these — closure/cycle test cases — lifts into `tests/auth/authz/test_role_mapping_store.py`.
- Test fixtures retargeted: `tests/conftest.py` drops the YAML temp-file write; replaces it with `AUTHZ_BOOTSTRAP_USER=alice` so the bootstrap seeds the admin user. The `_FIXTURE_YAML` strings in `tests/auth/test_session_dep.py` and `tests/auth/authz/test_authz_deps.py` are replaced with explicit `RoleMappingStore` calls inside their `_build_app` helpers (creates the `reader`, `writer`, `admin` roles via `await store.add_role(...)` + `add_group_to_role(...)` + `add_include(...)`).
- `pyproject.toml`: drop `pyyaml` from `dependencies`. Drop the `pyyaml` line.
- `CLAUDE.md`: rip out the YAML schema docs, the `AUTHZ_CONFIG_PATH` env var, the loader's mtime-cache description, the "robustness against bad edits" paragraph. Replace with: env vars (`AUTH_DB_PATH`, `AUTHZ_BOOTSTRAP_ROLE`, `AUTHZ_BOOTSTRAP_USER`), schema overview, mutator API list, bootstrap semantics.

Existing deployments upgrading: their `authz.yaml` stops being read; their previous role config disappears. They set `AUTHZ_BOOTSTRAP_USER=themselves` on the upgrade boot, get admin powers, recreate their roles via the mutator API. (For the iris deploy size — single-digit users — this is fine.)

## Error handling

| Failure | Behavior |
|---|---|
| `AUTH_DB_PATH` unwritable at boot | `sqlite3.OperationalError` from `connect`; app refuses to start (fail-loud, same as session store) |
| `AUTHZ_BOOTSTRAP_*` invalid identifier | Validated at install time; raises `RoleMappingError` and app refuses to start |
| Mutator: invalid identifier | `RoleMappingError` |
| Mutator: cycle in `add_include` | `RoleMappingError(f"cycle detected: ...")` |
| Mutator: FK violation (`add_include` for non-existent included_role; `remove_role` for a role still included) | Caught `sqlite3.IntegrityError`, re-raised as `RoleMappingError` |
| `get_mapping`: corrupt DB | `sqlite3.DatabaseError` propagates; route 500s |

## Testing

Three new files plus retargeting of existing ones.

### `tests/auth/authz/test_role_mapping_store.py` (NEW)

Covers the eight mutators against a tempfile DB:

- `add_role` then `get_mapping` → role appears in `RoleMapping.roles`.
- `add_role` is idempotent (second call no-ops, no error).
- `add_role` rejects names not matching `[a-zA-Z0-9_-]+`.
- `remove_role` cascades: role's groups/users/includes disappear from the DB.
- `remove_role` raises when another role includes it.
- `add_group_to_role`/`remove_group_to_role` round-trip.
- `add_user_to_role` lowercases the username; case-insensitive match in `get_mapping`.
- `add_include` rejects cycles (`A includes B`, `B includes A` → second call raises).
- `add_include` rejects undefined included_role (FK violation → `RoleMappingError`).
- `get_mapping` returns the same `RoleMapping` shape the existing dep system consumes; `closure` includes transitive memberships.

### `tests/auth/authz/test_authz_bootstrap.py` (NEW)

- First-install seeds: empty DB + `AUTHZ_BOOTSTRAP_USER=alice` → `authz_roles` has `admin` + `clickhouse_admin`; `authz_role_includes` has `(admin, clickhouse_admin)`; `authz_role_users` has `(admin, alice)`.
- Idempotent on second install: reset env to nothing, call `install_authz_schema` again → no changes (tables already exist).
- Operator deletes a row, restart: bootstrap doesn't restore (tables exist).
- `AUTHZ_BOOTSTRAP_USER` unset on fresh DB → no rows seeded; `get_mapping().roles` is empty.
- Custom `AUTHZ_BOOTSTRAP_ROLE=superuser` works: bootstrap creates `superuser` instead of `admin`, includes `clickhouse_admin`.
- Wiping the DB and pointing `AUTH_DB_PATH` at a new file re-triggers bootstrap (regression test for the file-vs-content distinction).
- The hardcoded `"clickhouse_admin"` matches `iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE` (string equality assertion to catch drift).

### Retargeted existing tests

- `tests/auth/test_session_dep.py`: `_build_app` builds a `RoleMappingStore` with the test's tempfile DB, calls `add_role` / `add_group_to_role` / `add_include` to construct the `reader → writer → admin` fixture, sets `app.state.authz_store`. Drops the YAML temp-file write.
- `tests/auth/authz/test_authz_deps.py`: same pattern.
- `tests/clickhouse/test_login_provisioning.py`: relies on the conftest bootstrap. Its YAML fixture string disappears (was in `tests/conftest.py`); the conftest now sets `AUTHZ_BOOTSTRAP_USER=alice` (matching `MOCK_USERNAME=alice`) so the seeded admin role admits the test user. Then in `test_login_provisioning.py`, the test user has the bootstrap admin role with the `clickhouse_admin` include — `require_clickhouse_admin` admits them.
- `tests/conftest.py`: drops the `_AUTHZ_FIXTURE` YAML write; switches `SESSION_DB_PATH=:memory:` to `AUTH_DB_PATH=:memory:`; sets `AUTHZ_BOOTSTRAP_USER=alice` so the conftest-provided test user is bootstrapped as admin.

### Deleted tests

- `tests/auth/authz/test_role_mapping.py` (YAML parser tests). The closure-and-cycle test cases lift into `test_role_mapping_store.py` against the new store.
- `tests/auth/test_authz_loader.py` (RoleMappingLoader mtime caching tests). Loader is gone.

## Public surface

After this work, `iris.auth` exports gain nothing visible at the package level — `RoleMappingStore` is accessed via `request.app.state.authz_store` inside routes. Routes still see `Session`, `OptionalSession`, `require_role`, `User`, `install`. No breaking change to that surface.

If a future admin route wants to call mutators, it does:

```python
@app.post("/admin/roles/{name}")
async def create_role(request: Request, name: str, _: Session = Depends(require_role("admin"))):
    await request.app.state.authz_store.add_role(name)
    return {"ok": True}
```

## Open follow-ups (not in this spec)

- HTTP routes exposing the mutator API. Future work; a small CSRF-protected admin section.
- Audit log: who changed which assignment when. Useful at scale; deferred.
- Bulk import endpoint (e.g., upload a YAML or JSON; convert to mutator calls under one transaction). Useful for migrating from external sources; deferred.
- Per-process in-memory cache of `RoleMapping` with version-column invalidation. Worth it if request volume goes up; not needed at ≤20 users.
