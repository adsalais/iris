# ClickHouse authorization module — design

**Date:** 2026-05-05
**Scope:** Add a self-contained `iris.clickhouse` package that provisions ClickHouse users, roles, grants, and row policies. Provides audit queries. Tested against a real ClickHouse server via `testcontainers-python` (Docker).
**Out of scope:** HTTP routes, integration with `iris.auth`, the runtime "execute query as user" helper, the call-site of `init_user_rights`. All deferred.

## Motivation

iris will eventually proxy SELECT/INSERT/UPDATE traffic to ClickHouse on behalf of its end users. That proxy needs each iris user to exist in ClickHouse with row-level policies derived from group membership, and a service identity that can `IMPERSONATE` any user to actually run their queries. This design covers the provisioning + audit half. The runtime impersonation half lands later, alongside the routes.

The module **must not depend on `iris.auth`** internals. It exposes plain-data inputs (username strings, group lists) and is invoked by future code that bridges auth → clickhouse.

## chdb verification (and pivot)

The original design proposed `chdb` as the test backend. A Phase-0 spike against `chdb==4.1.6` (embedding ClickHouse 26.1.2.1) found that **chdb's embedded server hardcodes `system.user_directories` to a single read-only `users_xml` directory**, regardless of `<user_directories>` configuration in `config.xml`. As a result:

- `CREATE USER` fails with `ACCESS_STORAGE_FOR_INSERTION_NOT_FOUND`.
- The same applies to `CREATE ROLE`, `CREATE ROW POLICY`, and any other DDL that mutates RBAC state.
- Users/roles/policies *can* be defined statically in `users.xml`, but cannot be created or modified at runtime via SQL.

That breaks the original premise. **chdb is dropped**; the test strategy uses `testcontainers-python` to spin up a real `clickhouse/clickhouse-server` Docker image. The design becomes simpler as a result: a single `clickhouse-connect` client backend, no Protocol abstraction.

## High-level architecture

```
src/iris/clickhouse/
├── __init__.py            # public surface
├── config.py              # ClickHouseSettings.from_env()
├── client.py              # build_client(settings) -> clickhouse_connect Client
├── identifiers.py         # validate_identifier, quote_identifier, quote_string, policy_name
├── bootstrap.py           # ensure_service_admin(client, settings)
├── users.py               # init_user_rights, USER_ROLE_SUFFIX, GROUP_ROLE_SUFFIX
├── grants.py              # grant_select_to_database, grant_insert_update_to_table
├── policies.py            # add_row_policy, revoke_row_policy
└── audit.py               # user_grants, role_grants, *_row_policies, etc.

tests/clickhouse/
├── conftest.py            # session-scoped ClickHouseContainer + per-test prefix
├── test_clickhouse_identifiers.py
├── test_clickhouse_settings.py
├── test_clickhouse_smoke.py        # Phase-0: every DDL we use, against the testcontainer
├── test_clickhouse_bootstrap.py
├── test_clickhouse_users.py
├── test_clickhouse_grants.py
├── test_clickhouse_policies.py
└── test_clickhouse_audit.py
```

(File basenames must be globally unique under `tests/` — see `CLAUDE.md`. The `test_clickhouse_*` prefix avoids collisions with existing `test_config.py`, `test_identity.py`, etc.)

Top-level guarantees:

- Operations are functions that take a `clickhouse_connect.driver.client.Client` as their first argument. No backend abstraction — clickhouse-connect *is* the backend.
- All operations are idempotent: `CREATE ... IF NOT EXISTS` for create statements, diff-then-grant/revoke for membership reconciliation. Re-running a function is always safe.
- Identifiers from external sources (usernames from auth, db/table/column from callers) are validated against `^[a-zA-Z0-9_]+$` and refused if non-conforming. Anything that would have to be escaped is treated as bad input, not coerced.
- Row-policy values are SQL string literals quoted via `quote_string`; their slugified form plus an 8-character hash of the raw value is embedded into the policy name to avoid collisions.

## Module structure

### Public surface (`iris.clickhouse.__init__`)

```python
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.client import build_client
from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.users import init_user_rights
from iris.clickhouse.grants import (
    grant_select_to_database, grant_insert_update_to_table,
)
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.audit import (
    user_grants, role_grants, user_role_memberships,
    user_row_policies, role_row_policies, table_row_policies,
)

__all__ = [
    "ClickHouseSettings", "build_client",
    "ensure_service_admin",
    "init_user_rights",
    "grant_select_to_database", "grant_insert_update_to_table",
    "add_row_policy", "revoke_row_policy",
    "user_grants", "role_grants", "user_role_memberships",
    "user_row_policies", "role_row_policies", "table_row_policies",
]
```

### Constants

`USER_ROLE_SUFFIX = "_USER"` and `GROUP_ROLE_SUFFIX = "_GRP"` as module constants in `users.py`. Hardcoded — no env var, no operator override.

## Settings

```python
@dataclass(frozen=True, slots=True)
class ClickHouseSettings:
    host: str
    port: int
    user: str                          # the service-admin login iris connects as
    password: str
    secure: bool                       # https
    verify: bool                       # TLS verification
    ca_cert_path: str | None
    service_admin_user: str            # equals `user` in normal deployments; kept distinct so the
                                       # IMPERSONATE/wildcard-policy grantee can differ from the
                                       # connecting login if an operator wants
    service_admin_role: str            # role granted to service_admin_user; wildcard-policy grantee

    @classmethod
    def from_env(cls) -> "ClickHouseSettings": ...
```

Validation at `from_env()` (mirrors `AuthSettings.from_env`):

- `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD` are required. `PORT` must parse as `int`.
- `CLICKHOUSE_SECURE` and `CLICKHOUSE_VERIFY` go through a strict `_get_bool` (rejects typos like `ture`).
- `CLICKHOUSE_SERVICE_ADMIN_USER` and `CLICKHOUSE_SERVICE_ADMIN_ROLE` are required and run through `validate_identifier`. Bad values → fail at boot.
- `CLICKHOUSE_CA_CERT_PATH` is optional (None when unset).

### Env vars (added to `.env` with comments)

```
# ClickHouse connection (server-side identity iris connects as)
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=iris_service
CLICKHOUSE_PASSWORD=replace-me
CLICKHOUSE_SECURE=true
CLICKHOUSE_VERIFY=true
# CLICKHOUSE_CA_CERT_PATH=/etc/ssl/certs/ca-bundle.crt

# ClickHouse: identity used for impersonation and as the wildcard-policy grantee.
# CLICKHOUSE_SERVICE_ADMIN_USER typically equals CLICKHOUSE_USER; the role is granted
# to that user at startup and is the grantee of all wildcard `USING 1` row policies.
CLICKHOUSE_SERVICE_ADMIN_USER=iris_service
CLICKHOUSE_SERVICE_ADMIN_ROLE=service_admin_role
```

`tests/conftest.py` populates the env vars to point at the testcontainers-managed ClickHouse instance once it has started. Per-test isolation comes from a UUID-prefixed namespace, not from per-test env overrides.

## Client

```python
import clickhouse_connect
from clickhouse_connect.driver.client import Client

def build_client(settings: ClickHouseSettings) -> Client:
    kwargs: dict[str, Any] = {
        "host": settings.host,
        "port": settings.port,
        "username": settings.user,
        "password": settings.password,
        "secure": settings.secure,
        "verify": settings.verify,
    }
    if settings.ca_cert_path:
        kwargs["ca_cert"] = settings.ca_cert_path
    return clickhouse_connect.get_client(**kwargs)
```

Operations type their first argument as `clickhouse_connect.driver.client.Client`. They use:

- `client.command(sql)` for DDL/DCL, with the SQL built from validated identifiers via the `identifiers` module — no parameter binding.
- `client.query(sql, parameters=...)` for DML/SELECT (audit functions), using ClickHouse's native `{name:Type}` placeholder binding.

Caller-facing usage (audit example):

```python
client.query(
    "SELECT * FROM system.grants WHERE user_name = {u:String}",
    parameters={"u": username},
)
```

## Identifier safety (`identifiers.py`)

```python
_IDENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")

class InvalidIdentifierError(ValueError): ...

def validate_identifier(name: str, *, kind: str) -> str:
    """Reject anything outside [a-zA-Z0-9_]+. Returns name unchanged on success.
    `kind` ('username', 'role', 'database', ...) is for error messages."""

def quote_identifier(name: str, *, kind: str) -> str:
    """Validate then backtick-quote. Since the regex blocks backticks, the
    quoted form is always safe to inline into DDL."""

def quote_string(value: str) -> str:
    """SQL string literal escaping: 'O''Brien'."""

def policy_name(database: str, table: str, role: str, value: str) -> str:
    """<database>_<table>_<role>_<slug>_<8charhash>. Slug is the value with
    non-[a-zA-Z0-9_] stripped to '_'; hash of the raw value disambiguates
    collisions ('EU/UK' vs 'EU UK')."""
```

`validate_identifier` is called at the entry point of every operation function on every external-source string (username, group names, db/table/column). That single guarantee combined with `quote_identifier` for inlining makes the DDL surface safe.

`quote_string` is needed for row-policy values: the user-supplied `value` is embedded into `USING column = '<value>'` and must be SQL-escaped.

## Operations

### `bootstrap.ensure_service_admin(client, settings) -> None`

Idempotent startup routine:

1. `CREATE ROLE IF NOT EXISTS <service_admin_role>`.
2. `GRANT <service_admin_role> TO <service_admin_user>` (idempotent in CH).

Bootstrap presumes the service admin *user* already exists (operator-provisioned: iris must already be authenticating as it). If the user is missing, the GRANT will raise — that's the right behavior, since the deployment is misconfigured.

`service_admin_user` and `service_admin_role` were already validated at `from_env()`, so this routine doesn't re-validate.

### `users.init_user_rights(client, *, username, groups, settings) -> None`

```python
def init_user_rights(
    client: Client,
    *,
    username: str,
    groups: list[str],
    settings: ClickHouseSettings,
) -> None:
```

Steps:

1. `validate_identifier(username, kind="username")`. For each `g` in `groups`: `validate_identifier(g, kind="group")`.
2. `CREATE USER IF NOT EXISTS <username> IDENTIFIED WITH no_password`.
3. Per-user role:
   - `CREATE ROLE IF NOT EXISTS <username>_USER`.
   - `GRANT <username>_USER TO <username>` (idempotent).
4. Group reconcile:
   - Query current grants:
     ```sql
     SELECT granted_role_name FROM system.role_grants
     WHERE user_name = {u:String}
     ```
     Filter to roles ending in `_GRP`. Call this set `current`.
   - `desired = {g + "_GRP" for g in groups}`.
   - For `r in current - desired`: `REVOKE <r> FROM <username>`.
   - For `r in desired - current`: `CREATE ROLE IF NOT EXISTS <r>`; `GRANT <r> TO <username>`.
5. `GRANT IMPERSONATE ON <username> TO <service_admin_user>`. Idempotent.

The per-user role (`<username>_USER`) is intentionally *not* part of the reconcile — it's the user's own identity, distinct from group membership. The reconcile filter (`endswith("_GRP")`) excludes it.

### `grants.grant_select_to_database(client, *, database, role) -> None`

```sql
GRANT SELECT ON `<database>`.* TO `<role>`
```

Validates both. Idempotent.

### `grants.grant_insert_update_to_table(client, *, database, table, role) -> None`

Two `command` calls:

```sql
GRANT INSERT ON `<database>`.`<table>` TO `<role>`;
GRANT ALTER UPDATE ON `<database>`.`<table>` TO `<role>`;
```

Each idempotent.

### `policies.add_row_policy(client, *, database, table, column, role, value, settings) -> None`

Two `command` calls:

```sql
CREATE ROW POLICY IF NOT EXISTS `<policy_name(db, tbl, role, value)>`
  ON `<database>`.`<table>`
  FOR SELECT USING `<column>` = '<value-escaped>'
  TO `<role>`;

CREATE ROW POLICY IF NOT EXISTS `<database>_<table>_<service_admin_role>`
  ON `<database>`.`<table>`
  FOR SELECT USING 1
  TO `<service_admin_role>`;
```

The wildcard policy is a constant per `(database, table, service_admin_role)` triple — `IF NOT EXISTS` makes repeated calls free.

`value` is unrestricted (`str`) so callers can express any literal cleanly; it goes through `quote_string` before substitution. `column` is a strict identifier.

### `policies.revoke_row_policy(client, *, database, table, role, value) -> None`

```sql
DROP ROW POLICY IF EXISTS `<policy_name(db, tbl, role, value)>` ON `<database>`.`<table>`
```

The wildcard service-admin policy is *not* dropped — it's a singleton per table and other policies on the same table may still reference it. (Note: dropped the `column` parameter from the original sketch — the policy name doesn't depend on it, so it was unused.)

## Audit (`audit.py`)

Six functions, each a thin wrapper around a `client.query(...).named_results()` call. Returns `list[dict[str, Any]]`; not converted to dataclasses (operators benefit from the raw `system.*` column shape, and the schema may evolve).

```python
def user_grants(client, *, username) -> list[dict]
    # SELECT * FROM system.grants WHERE user_name = {u:String}

def role_grants(client, *, role) -> list[dict]
    # SELECT * FROM system.grants WHERE role_name = {r:String}

def user_role_memberships(client, *, username) -> list[dict]
    # SELECT * FROM system.role_grants WHERE user_name = {u:String}

def user_row_policies(client, *, username) -> list[dict]
    # JOIN system.row_policies with role_grants for this user

def role_row_policies(client, *, role) -> list[dict]
    # SELECT * FROM system.row_policies WHERE has(apply_to_list, {r:String})

def table_row_policies(client, *, database, table) -> list[dict]
    # SELECT * FROM system.row_policies
    # WHERE database = {d:String} AND table = {t:String}
```

Each function calls `validate_identifier` on its inputs, even though the SQL uses `{name:String}` binding (defense in depth — caller bugs that pass `None` or weird strings get a clean error).

## Tests

### Container fixture

`tests/clickhouse/conftest.py` defines a **session-scoped** `ClickHouseContainer` fixture. Pulling and starting the image takes ~5–10 seconds; reusing it across the suite keeps the suite fast.

```python
@pytest.fixture(scope="session")
def ch_container():
    with ClickHouseContainer("clickhouse/clickhouse-server:24") as ch:
        yield ch

@pytest.fixture
def ch_settings(ch_container, monkeypatch):
    # populate env vars from the running container, then ClickHouseSettings.from_env()
    ...
    return ClickHouseSettings.from_env()

@pytest.fixture
def ch_client(ch_settings):
    client = build_client(ch_settings)
    ensure_service_admin(client, ch_settings)
    yield client
    client.close()

@pytest.fixture
def prefix():
    """Per-test UUID prefix. Tests apply it to all entity names so concurrent
    tests in the same session don't collide."""
    return "t_" + uuid.uuid4().hex[:8]
```

Tests use `prefix` to namespace usernames, role names, databases, tables. State accumulates within the session but doesn't interfere because names are unique. No teardown is needed (image is dropped on session exit).

### Per-module test focus

- `test_clickhouse_identifiers.py` — `validate` / `quote` / `policy_name` slug+hash behavior, including collision resolution for values that share a slug.
- `test_clickhouse_settings.py` — env parsing, validation (missing required vars, bad identifiers, typo'd booleans, non-int port).
- `test_clickhouse_smoke.py` — **Phase-0 verification.** Runs each DDL the module uses (`CREATE USER`, `CREATE ROLE`, `GRANT`, `REVOKE`, `GRANT IMPERSONATE`, `CREATE ROW POLICY`, `DROP ROW POLICY`, every audit `SELECT FROM system.*`) against the testcontainer with the service admin login. Pins the exact `IMPERSONATE` syntax our installed CH expects.
- `test_clickhouse_bootstrap.py` — service admin role created; granted to user; idempotent on re-run.
- `test_clickhouse_users.py` — full `init_user_rights` flow: creation, group reconcile (start `[a, b]`, reconcile to `[b, c]` → `a_GRP` revoked, `c_GRP` granted, `b_GRP` untouched), IMPERSONATE grant, idempotency.
- `test_clickhouse_grants.py` — both grant functions, idempotency.
- `test_clickhouse_policies.py` — add + revoke; wildcard policy presence after `add_row_policy`; wildcard policy *not* dropped by `revoke_row_policy`.
- `test_clickhouse_audit.py` — every audit function returns the expected rows for a known fixture state.

### Top-level `tests/conftest.py`

No additional env defaults are needed for the existing test suite. The clickhouse fixtures live entirely under `tests/clickhouse/conftest.py` and only activate when a clickhouse test imports them. Tests outside `tests/clickhouse/` never start the container.

## Open verification items

- **`GRANT IMPERSONATE` syntax.** ClickHouse may accept `GRANT IMPERSONATE ON <user> TO <admin>` *or* `GRANT IMPERSONATE(<user>) ON *.* TO <admin>` depending on version. The smoke test pins whichever form the installed CH accepts; if it's the latter, `init_user_rights` step 5 adapts.
- **clickhouse-connect query result shape.** Audit functions call `result.named_results()` to get `list[dict]`. If the helper's exact name differs in the installed version, the audit module imports and uses it accordingly. The smoke test's audit-side checks pin this.

## Deliverables (this PR)

- `src/iris/clickhouse/` package as outlined above.
- `tests/clickhouse/` test suite as outlined above.
- `.env` updated with the new `CLICKHOUSE_*` block, comments, and example values (no secrets).
- `pyproject.toml`:
  - **Removes** the unused `chdb>=4.1.6` runtime dep.
  - Adds `clickhouse-connect` to runtime deps.
  - Adds `testcontainers[clickhouse]` to dev deps.
- `CLAUDE.md` gets a "ClickHouse module" section mirroring the "Authentication" section's style.

## Non-deliverables (explicitly deferred)

- Routes that call any of these functions.
- Any wiring between `iris.auth` and `iris.clickhouse`.
- The runtime `execute_as(username, sql)` helper for impersonating queries.
- Connection pooling, multi-worker session sharing, migration tooling.
