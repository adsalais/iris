# Bootstrap rework + iris_global_admin sentinel

Replace the current bootstrap (which seeds a single admin user) with a two-channel scheme that bootstraps both an admin user role AND an admin group role from CH-prefixed env vars. Introduce a sentinel CH role (`iris_global_admin`) that wildcard row policies attach to, removing the configured `CLICKHOUSE_SERVICE_ADMIN_ROLE` concept. Drop `iris.auth.bootstrap` (move to `iris.clickhouse.bootstrap`) and the lazy imports in Session methods, both of which were workarounds for an avoidable module-load cycle.

## Why

Three problems in the current setup, all stemming from the same root:

**1. Single-channel bootstrap is brittle.** Today `IRIS_BOOTSTRAP_USER` seeds one user as admin. If the operator wants admin to flow through an IdP group (say, every member of `iris_admin` should be admin), they have to log in as the bootstrap user, manually grant CH privileges to the group's `<group>_GRP` role, and remember to also wire `iris_global_admin` (or whatever the wildcard role is). Group-based admin should be a first-class deployment knob.

**2. The wildcard row-policy role is misconfigured by design.** The current `CLICKHOUSE_SERVICE_ADMIN_ROLE` is a single configured role; restrictive policies created via `add_row_policy` attach a `USING 1` rule for that role only. Global admins and DB admins don't automatically receive the wildcard, so a user holding `<X>_DBADMIN` but not the configured service-admin role sees zero rows on any table that has a restrictive policy. The fix is to attach wildcards to the actual admin tiers (`iris_global_admin` for global, `<X>_DBADMIN` for per-DB), not to a separately-configured role.

**3. The lazy imports inside Session methods leak structural smell.** `iris.auth.identity.AuthSession.query_as_user` does `from iris.clickhouse.handle import query_as_user_impl` inside the method body to dodge a module-load cycle. The cycle is `iris.auth.bootstrap → iris.clickhouse → iris.auth.bootstrap`, which only exists because `bootstrap_admin` is in `iris.auth` and `iris.clickhouse.__init__` re-exports `install`. Both are easy to fix: move bootstrap to `iris.clickhouse` (the env var is `CLICKHOUSE_*` anyway), and stop re-exporting `install` from the package root.

## Scope

In:
- New env vars: `CLICKHOUSE_ADMIN_USER` (replaces `IRIS_BOOTSTRAP_USER`), `CLICKHOUSE_ADMIN_GROUP` (new).
- Drop env vars: `CLICKHOUSE_SERVICE_ADMIN_USER` (merged into `CLICKHOUSE_USER`), `CLICKHOUSE_SERVICE_ADMIN_ROLE` (replaced by sentinel role).
- Sentinel CH role `iris_global_admin` — created at boot, granted to bootstrapped admin user/group roles, target of wildcard row policies.
- Bootstrap behavior: at boot, create both `<user>_USER` (admin) and `<group>_GRP` (admin) when their env vars are set; grant `iris_global_admin` to both; reuse `CURRENT GRANTS` fallback for the testcontainer privilege envelope.
- `add_row_policy` change: wildcards attach to `iris_global_admin` AND `<database>_DBADMIN` (per call). Drop the configured-service-role attachment.
- File moves: `iris.auth.bootstrap` → `iris.clickhouse.bootstrap` (merged with `ensure_service_admin`). Delete `iris.auth.bootstrap`.
- Module hygiene: drop `install` from `iris.clickhouse.__init__`'s re-exports (callers do `from iris.clickhouse.install import install`); replace lazy imports in `iris.auth.identity` Session methods with top-level imports.

Out:
- Login-time reconciliation of out-of-band admin promotions. Operator who runs raw `GRANT ALL ON *.* TO foo_USER WITH GRANT OPTION` outside iris must also `GRANT iris_global_admin TO foo_USER` to see row-policy-protected tables. Documented; revisit if it bites.
- Renaming `iris.auth.session.py` / `iris.auth.sessions.py` (still confusing, but separate cleanup).
- Backfill of `iris_global_admin` grants for existing admins after upgrade. The deployment runbook says "stop iris, wipe `AUTH_DB_PATH`, re-set `CLICKHOUSE_ADMIN_USER`, restart" — same as the prior CH-only-authz migration. No in-place migrator.
- Multi-database wildcards across DB admins. A user who's DB admin of `finance` and `hr` separately holds two DBADMIN roles; each table's wildcard attaches to its DB's role; correct by construction.

## Decisions

### Env-var surface

| Variable | Status | Meaning |
|---|---|---|
| `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` | unchanged | iris's CH connection identity. Also the IMPERSONATE grantee — there's no longer a separate `SERVICE_ADMIN_USER`. |
| `CLICKHOUSE_HOST` / `_PORT` / `_SECURE` / `_VERIFY` / `_CA_CERT_PATH` | unchanged | connection params |
| `CLICKHOUSE_SERVICE_ADMIN_USER` | **dropped** | use `CLICKHOUSE_USER` |
| `CLICKHOUSE_SERVICE_ADMIN_ROLE` | **dropped** | replaced by managed sentinel `iris_global_admin` |
| `IRIS_BOOTSTRAP_USER` | **renamed** | → `CLICKHOUSE_ADMIN_USER` |
| `CLICKHOUSE_ADMIN_USER` | **new** | IdP username of bootstrap admin (e.g., `alice`). When set, iris creates `alice_USER` with full admin grants at boot. |
| `CLICKHOUSE_ADMIN_GROUP` | **new** | IdP group name of bootstrap admins (e.g., `iris_admin`). When set, iris creates `iris_admin_GRP` with full admin grants at boot. |

Both `CLICKHOUSE_ADMIN_USER` and `CLICKHOUSE_ADMIN_GROUP` are independently optional. A deployment with only the user is the v1 model. A deployment with only the group is OAuth-friendly (no need to configure individual admins; manage via IdP group membership). A deployment with both gives a fixed seed admin plus a flexible group. A deployment with neither leaves CH unbootstrapped — only useful when CH is pre-configured externally.

### Sentinel role: `iris_global_admin`

A CH role with a fixed iris-owned name. **Holds no privileges itself** — its sole purpose is to be the grantee of wildcard row policies, and to be the role inheritance target that ties together every iris-recognised "admin user/group".

Lifecycle:
- Created at boot (idempotent, `CREATE ROLE IF NOT EXISTS iris_global_admin`).
- Granted to every bootstrap user role (`<user>_USER`) and bootstrap group role (`<group>_GRP`).
- Never dropped, even if both env vars become unset on a future boot. Operator who wants to remove admin privileges must drop the user/group role, not the sentinel.
- Tests that rely on "no admin exists" (e.g., the existing `test_bootstrap_creates_admin_when_absent`) clear matching `_USER` roles before running.

The detection rule for "is this user globally admin" stays exactly as today: `derive_rights` checks `system.grants` for `ROLE ADMIN at global scope with grant_option=1` on any role in the user's effective set. The sentinel doesn't change this — it carries no admin grants itself, only the wildcard policies. Granting `iris_global_admin` to a role does NOT make that role admin in any sense iris's authorization layer recognises.

### Bootstrap flow at iris launch

`iris.clickhouse.bootstrap.bootstrap_admin(client, *, admin_user: str | None, admin_group: str | None)` (new signature) is called from `iris.clickhouse.install.install` after `ensure_service_admin`. The function:

1. `CREATE ROLE IF NOT EXISTS iris_global_admin` (always, no env-var gating).
2. If `admin_user` is supplied AND no `<user>_USER` role currently holds the admin marker (ROLE ADMIN+WGO at global scope, suffix `_USER`):
   - `CREATE ROLE IF NOT EXISTS <admin_user>_USER`
   - Grant full admin: `GRANT ALL ON *.* TO <admin_user>_USER WITH GRANT OPTION` (with `CURRENT GRANTS` fallback for the testcontainer's NAMED COLLECTION ADMIN limitation, same logic as today's `bootstrap_admin`).
   - `GRANT iris_global_admin TO <admin_user>_USER`.
3. If `admin_group` is supplied AND no `<group>_GRP` role currently holds the admin marker (suffix `_GRP`):
   - `CREATE ROLE IF NOT EXISTS <admin_group>_GRP`
   - Grant full admin (same `CURRENT GRANTS` fallback).
   - `GRANT iris_global_admin TO <admin_group>_GRP`.

The "no admin marker" check is suffix-scoped: the user-channel only inspects `_USER`-suffixed roles and the group-channel only inspects `_GRP`-suffixed roles. This means a deployment with only `CLICKHOUSE_ADMIN_GROUP` set on first boot will correctly seed the group role even if some unrelated `_USER` role happens to exist. Both channels are independently idempotent: re-running with an existing admin in the channel is a no-op.

When alice (whose IdP username matches `CLICKHOUSE_ADMIN_USER`) logs in for the first time, `init_user_rights` does its existing work: creates the CH user `alice`, ensures `alice_USER` role exists (idempotent — already created), grants `alice_USER` to the CH user, ensures group roles for her IdP groups exist, and grants those. `alice_USER` already holds admin grants and `iris_global_admin` from the bootstrap, so alice's `derive_rights` returns `is_admin=True` and her effective role set includes `iris_global_admin`. Same flow for bob (whose IdP groups include `CLICKHOUSE_ADMIN_GROUP=iris_admin`): his CH user gets `iris_admin_GRP` granted, which already holds admin + `iris_global_admin`, so bob is admin via the group path.

### Row policies: where wildcards attach

`add_row_policy(database, table, column, role, value, settings)` changes its emitted DDL. Today it emits two `CREATE ROW POLICY` statements: the restrictive one for the target role, and a wildcard for `settings.service_admin_role`. After the change:

| Statement | Purpose |
|---|---|
| `CREATE ROW POLICY <db>_<table>_<role>_<slug>_<hash> ON <db>.<table> USING <expr> TO <role>` | The restrictive policy the caller asked for. Unchanged. |
| `CREATE ROW POLICY <db>_<table>_iris_global_admin ON <db>.<table> USING 1 TO iris_global_admin` | Wildcard for global admins. Idempotent — same name on each call for the same table. |
| `CREATE ROW POLICY <db>_<table>_<db>_DBADMIN ON <db>.<table> USING 1 TO <db>_DBADMIN` | Wildcard for the DB admin of this database. Idempotent — same name on each call. |

The two wildcards have stable, deterministic names so re-running `add_row_policy` for the same table produces no churn. CH's `CREATE ROW POLICY IF NOT EXISTS` makes them idempotent. The settings argument no longer needs `service_admin_role` — drop it from `ClickHouseSettings`.

`revoke_row_policy(database, table, role, value)` continues to drop only the restrictive policy by name (matching the slug + hash). The two wildcard policies stay. They're per-table, attach to roles iris always has, and removing them would break admin visibility.

If an operator drops the database via `DatabaseAdminSession.delete_database`, `DROP DATABASE IF EXISTS` cascades to drop all row policies in that database (CH semantics) — the per-table wildcards die with the table.

### Module / file layout after the change

```
src/iris/auth/
├── __init__.py               # public surface unchanged in shape (no bootstrap_admin export — moved)
├── identity.py               # User, UserSession, AuthSession + Session subclass hierarchy with TOP-LEVEL imports from iris.clickhouse.handle
├── session.py                # Rights, EMPTY_RIGHTS — unchanged
├── sessions.py               # SessionStore — unchanged
├── deps.py                   # alias deps — unchanged
├── exceptions.py             # unchanged
├── csrf.py                   # unchanged
├── rate_limit.py             # unchanged
├── routes.py                 # /login, /logout, /api/whoami — unchanged
└── providers/                # unchanged
```

```
src/iris/clickhouse/
├── __init__.py               # public surface — drops `install` re-export
├── audit.py                  # unchanged
├── bootstrap.py              # ensure_service_admin (existing) + bootstrap_admin (moved from iris.auth)
├── client.py                 # unchanged
├── config.py                 # ClickHouseSettings drops service_admin_user / service_admin_role fields
├── grants.py                 # unchanged
├── handle.py                 # *_impl functions — unchanged
├── identifiers.py            # unchanged
├── install.py                # imports bootstrap_admin from iris.clickhouse.bootstrap; reads CLICKHOUSE_ADMIN_USER/_GROUP from env (or AuthSettings)
├── policies.py               # add_row_policy now emits 3 statements, drops service_admin_role param
├── rights.py                 # unchanged
└── users.py                  # unchanged
```

Deleted:
- `src/iris/auth/bootstrap.py` (moved to `iris.clickhouse.bootstrap`)
- `tests/auth/test_bootstrap_admin.py` if any (the existing tests live under `tests/clickhouse/test_bootstrap_admin.py` already; just retarget its import path)

### Cycle resolution: lazy imports → top-level

After the moves above, the module-load graph is:

```
iris.auth.identity → iris.clickhouse.handle  (Session methods need *_impl functions; top-level)
iris.clickhouse.install → iris.auth.sessions (SessionStore typing; top-level — runs late)
iris.clickhouse.bootstrap → iris.clickhouse.{identifiers,users,grants,policies}  (no auth imports)
iris.auth.deps → no clickhouse imports at module load (just Session classes from identity)
```

The cycle that today forces lazy imports — `iris.auth.bootstrap` (top-level) → `iris.clickhouse.identifiers/users` → `iris.clickhouse.__init__` → `iris.clickhouse.install` → `iris.auth.bootstrap` — disappears because:
1. `iris.auth.bootstrap` no longer exists.
2. `iris.clickhouse.__init__` no longer imports `install` (`install` is a function, not a value type; callers import the submodule directly).

`iris.auth.identity`'s top-level imports of `iris.clickhouse.handle.*_impl` are clean: when iris.auth loads, it triggers iris.clickhouse.__init__ (loading audit/grants/handle/policies/rights — no auth imports), then iris.auth.identity's body runs (using the loaded *_impl symbols). No cycle.

### `ClickHouseSettings` cleanup

The dataclass loses two fields:
- `service_admin_user` — no longer separate from `user`. Helpers that need "the user iris connects as" use `settings.user`.
- `service_admin_role` — no longer configured. Helpers that need "the wildcard role" use the literal string `"iris_global_admin"` (one place, in `policies.py`'s `add_row_policy`).

`AuthSettings` loses:
- `bootstrap_user` — superseded by reading `CLICKHOUSE_ADMIN_USER` directly in `iris.clickhouse.install`. The auth layer no longer cares about the bootstrap concept.

`AuthSettings.from_env` no longer reads `IRIS_BOOTSTRAP_USER`. `iris.app.build_app` doesn't pass anything; `iris.clickhouse.install` reads `CLICKHOUSE_ADMIN_USER` and `CLICKHOUSE_ADMIN_GROUP` from `os.environ` directly, near the top of `install(app)`.

### Rights derivation: unchanged

`derive_rights` already computes admin from `system.grants` (ROLE ADMIN+WGO at global scope). The sentinel `iris_global_admin` doesn't carry that grant — it's just a marker that wildcard policies attach to. So the admin-detection logic doesn't change, and the sentinel doesn't appear in any `Rights` field.

### Test refactor

- `tests/clickhouse/test_bootstrap_admin.py` — already in the right location. Update the import to `from iris.clickhouse.bootstrap import bootstrap_admin`. Add tests for the group channel (`bootstrap_admin(client, admin_group="iris_admin")` creates `iris_admin_GRP` with admin + `iris_global_admin`).
- `tests/clickhouse/test_clickhouse_settings.py` — drop assertions about `service_admin_user`/`service_admin_role` env-var handling. The settings dataclass loses those fields.
- `tests/clickhouse/conftest.py` — keep granting iris_svc its current privilege set; no change needed (the svc user is now the only "admin role" iris uses internally for management).
- `tests/clickhouse/test_clickhouse_policies.py` — assertions about the second wildcard CREATE need to switch from `<settings.service_admin_role>` to `iris_global_admin`. Add an assertion for the third statement (`<database>_DBADMIN` wildcard).
- New tests covering: row policy targets a regular role; DB admin queries the table and sees all rows via the DBADMIN wildcard; global admin queries and sees all rows via the iris_global_admin wildcard.
- New test: bootstrap with `admin_group` only creates `<group>_GRP` admin role and grants iris_global_admin.
- Update `tests/clickhouse/test_install.py` to drop assertions about the deleted env vars and check the new bootstrap path.

## Migration / rollout

Big-bang, same shape as the prior two refactors. Operator runbook for the upgrade:

1. Stop iris.
2. Replace env vars in the deployment:
   - Drop `IRIS_BOOTSTRAP_USER`, `CLICKHOUSE_SERVICE_ADMIN_USER`, `CLICKHOUSE_SERVICE_ADMIN_ROLE`.
   - Set `CLICKHOUSE_ADMIN_USER` and/or `CLICKHOUSE_ADMIN_GROUP` to taste.
3. Optional but recommended: wipe `AUTH_DB_PATH` (drops sessions, forces all users to log in again).
4. Optional: reset CH RBAC state (drop iris-managed roles, drop the old `service_admin_role`, drop wildcard row policies that target it). Otherwise the old wildcard role stays harmlessly in CH; operators can drop it manually later.
5. Start iris. Bootstrap creates `iris_global_admin`, the user role, and/or the group role. Existing tier roles (`<X>_DBADMIN`/`_DBWRITER`/`_DBREADER`) and existing tier-grant memberships are unchanged.

There is no automated migration. The CH state from the previous deployment is mostly compatible — only the wildcard row policies pointing at the old `CLICKHOUSE_SERVICE_ADMIN_ROLE` become stale (they still grant `USING 1` on the old role, but no user holds that role anymore, so they have no effect). `add_row_policy` calls after the upgrade emit the new wildcards alongside.

## Open risks

- **Out-of-band admin promotion** drops the user out of `iris_global_admin`'s membership. If an operator runs raw `GRANT ALL ON *.* TO foo_USER WITH GRANT OPTION` outside iris's bootstrap path, foo gets admin grants but not `iris_global_admin`. derive_rights still returns `is_admin=True`, but row-policy wildcards keyed on `iris_global_admin` don't apply, so foo can't see rows on tables that have any restrictive policy. Mitigation: documentation; run `GRANT iris_global_admin TO foo_USER` alongside the admin grant. Out of scope to auto-detect.
- **Sentinel role lock-in**: the literal string `"iris_global_admin"` is hardcoded in `policies.py` and `bootstrap.py`. Renaming requires updating both places. Acceptable because operators don't see this name.
- **Test container privilege envelope**: `CURRENT GRANTS` fallback continues to mask the real production-only `GRANT ALL` path. The unit tests for `bootstrap_admin` exercise the fallback, not the production branch. Same trade-off as today.
- **CLICKHOUSE_USER == iris connection identity == IMPERSONATE grantee**: dropping `CLICKHOUSE_SERVICE_ADMIN_USER` removes a deployment escape hatch where ops wanted iris to log in as a non-admin user but impersonate via a separate admin role. We don't think this was used in practice, but document the loss in the changelog.
- **Wildcard policies persist after the last restrictive policy is revoked.** Once `add_row_policy` has run for a table, `iris_global_admin` and `<X>_DBADMIN` wildcards stay for that table even if every restrictive policy is later revoked. Effect: the table is visible only to admins (CH default-denies once any policy exists, even a permissive one). This matches today's behavior — the existing `service_admin_role` wildcard also sticks around. If a future operation wants to fully reset a table to "no policies, all grant-holders see everything", it has to drop the wildcards too. Out of scope for this rework; revisit if needed.
