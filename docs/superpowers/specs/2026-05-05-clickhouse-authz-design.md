# ClickHouse authorization module — design

**Date:** 2026-05-05
**Scope:** Add a self-contained `iris.clickhouse` package that provisions ClickHouse users, roles, grants, and row policies. Provides audit queries. Tested against chdb in-process; supports an external ClickHouse server for production.
**Out of scope:** HTTP routes, integration with `iris.auth`, the runtime "execute query as user" helper, the call-site of `init_user_rights`. All deferred.

## Motivation

iris will eventually proxy SELECT/INSERT/UPDATE traffic to ClickHouse on behalf of its end users. That proxy needs each iris user to exist in ClickHouse with row-level policies derived from group membership, and a service identity that can `IMPERSONATE` any user to actually run their queries. This design covers the provisioning + audit half. The runtime impersonation half lands later, alongside the routes.

The module **must not depend on `iris.auth`** internals. It exposes plain-data inputs (username strings, group lists) and is invoked by future code that bridges auth → clickhouse.

## High-level architecture

```
src/iris/clickhouse/
├── __init__.py            # public surface
├── config.py              # ClickHouseSettings.from_env() + sub-settings
├── client.py              # Client Protocol + build_client(settings) factory
├── identifiers.py         # validate_identifier, quote_identifier, quote_string, policy_name
├── backends/
│   ├── chdb.py            # ChdbClient — wraps chdb.session.Session
│   └── connect.py         # ConnectClient — wraps clickhouse_connect.Client
├── bootstrap.py           # ensure_service_admin(client, settings)
├── users.py               # init_user_rights
├── grants.py              # grant_select_to_database, grant_insert_update_to_table
├── policies.py            # add_row_policy, revoke_row_policy
└── audit.py               # user_grants, role_grants, *_row_policies, etc.

tests/clickhouse/
├── conftest.py            # chdb_client fixture
├── test_settings.py
├── test_identifiers.py
├── test_chdb_smoke.py     # Phase-0: every DDL we use, against chdb
├── test_bootstrap.py
├── test_users.py
├── test_grants.py
├── test_policies.py
├── test_audit.py
└── test_external.py       # gated by CLICKHOUSE_TEST_EXTERNAL=1
```

Top-level guarantees:

- The two backends share a `Client` protocol; everything else is backend-agnostic.
- chdb runs as `chdb.session.Session(<persistent_dir>)` so users/roles/policies survive across queries within a process.
- All operations are idempotent: `CREATE ... IF NOT EXISTS` for create statements, diff-then-grant/revoke for membership reconciliation. Re-running a function is always safe.
- Identifiers from external sources (usernames from auth, db/table/column from callers) are validated against `^[a-zA-Z0-9_]+$` and refused if non-conforming. Anything that would have to be escaped is treated as bad input, not coerced.
- Row-policy values are SQL string literals quoted via `quote_string`; their slugified form plus an 8-character hash of the raw value is embedded into the policy name to avoid collisions.

## Module structure

### Public surface (`iris.clickhouse.__init__`)

```python
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.client import Client, build_client
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
    "ClickHouseSettings", "Client", "build_client",
    "ensure_service_admin",
    "init_user_rights",
    "grant_select_to_database", "grant_insert_update_to_table",
    "add_row_policy", "revoke_row_policy",
    "user_grants", "role_grants", "user_role_memberships",
    "user_row_policies", "role_row_policies", "table_row_policies",
]
```

### Constants

`USER_ROLE_SUFFIX = "_USER"` and `GROUP_ROLE_SUFFIX = "_GRP"` as module constants in `users.py` (and re-imported where needed). Hardcoded — no env var, no operator override.

## Settings

```python
@dataclass(frozen=True, slots=True)
class ChdbSettings:
    data_path: str

@dataclass(frozen=True, slots=True)
class ExternalSettings:
    host: str
    port: int
    user: str
    password: str
    secure: bool
    verify: bool = True
    ca_cert_path: str | None = None

@dataclass(frozen=True, slots=True)
class ClickHouseSettings:
    backend: Literal["chdb", "external"]
    service_admin_user: str
    service_admin_role: str
    chdb: ChdbSettings | None
    external: ExternalSettings | None

    @classmethod
    def from_env(cls) -> "ClickHouseSettings": ...
```

Validation at `from_env()` (mirrors `AuthSettings.from_env`):

- `CLICKHOUSE_BACKEND` must be `chdb` or `external`. Anything else → fail at boot.
- `service_admin_user` and `service_admin_role` are run through `validate_identifier` once at boot. Bad values → fail loudly.
- If `external`: `HOST`, `PORT`, `USER`, `PASSWORD` are required. `SECURE`/`VERIFY` parsed via `_get_bool` (rejects typos like `ture`).
- If `chdb`: `CHDB_DATA_PATH` is required. The directory and parents are created if missing.
- The opposite backend's settings block is `None`.

### Env vars (added to `.env` with comments)

```
# ClickHouse: backend selection (chdb embeds CH in-process; external talks to a real CH)
CLICKHOUSE_BACKEND=chdb              # chdb | external

# Identity used for impersonation and as the wildcard-policy grantee. Both backends.
CLICKHOUSE_SERVICE_ADMIN_USER=iris_service
CLICKHOUSE_SERVICE_ADMIN_ROLE=service_admin_role

# chdb backend (CLICKHOUSE_BACKEND=chdb) — persistent directory for the embedded CH state.
# Wiped on directory delete; gitignore it.
CHDB_DATA_PATH=./var/chdb

# External backend (CLICKHOUSE_BACKEND=external)
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=iris_service
CLICKHOUSE_PASSWORD=replace-me
CLICKHOUSE_SECURE=true
CLICKHOUSE_VERIFY=true
# CLICKHOUSE_CA_CERT_PATH=/etc/ssl/certs/ca-bundle.crt
```

`tests/conftest.py` adds module-scope `os.environ.setdefault("CLICKHOUSE_BACKEND", "chdb")` plus a default `CHDB_DATA_PATH` (overridden per-test via fixture), so importing `iris.clickhouse` in tests Just Works regardless of the developer's `.env`.

## Backend abstraction

```python
from clickhouse_connect.driver.query import QueryResult
from clickhouse_connect.driver.summary import QuerySummary

class Client(Protocol):
    def query(
        self, sql: str, parameters: dict[str, Any] | None = None
    ) -> QueryResult: ...

    def command(self, sql: str) -> QuerySummary:
        """DDL/DCL only. No parameter binding — caller builds the SQL string
        from validated identifiers via the identifiers module."""

    def close(self) -> None: ...

def build_client(settings: ClickHouseSettings) -> Client:
    if settings.backend == "chdb":
        return ChdbClient(settings.chdb)
    return ConnectClient(settings.external)
```

The signature deliberately mirrors `clickhouse_connect.Client` so callers can use idiomatic `{name:Type}` parameter binding for DML:

```python
client.query(
    "SELECT * FROM {table:Identifier} WHERE date >= {v1:DateTime} AND s ILIKE {v2:String}",
    parameters={"table": "my_table", "v1": d, "v2": "she'd say"},
)
```

`command()` deliberately accepts no parameters dict. DDL/DCL is built from validated identifiers via the `identifiers` module — that's the single safety contract for DDL.

### `ConnectClient`

Literal passthrough. `query` and `command` delegate to the inner `clickhouse_connect.Client.query` / `.command`. Constructor takes `ExternalSettings` and instantiates via `clickhouse_connect.get_client(host=..., port=..., username=..., password=..., secure=..., verify=..., ca_cert=...)`.

### `ChdbClient`

Wraps `chdb.session.Session(data_path)`.

- `command(sql)`: runs `session.query(sql)`, discards output, returns a synthesized `QuerySummary`. Exceptions from chdb propagate unchanged.
- `query(sql, parameters)`: client-side substitutes `{name:Type}` placeholders into the SQL using `clickhouse_connect.driver.binding.bind_query_params` (or its public equivalent), then runs `session.query(rendered_sql, "JSONEachRow")`, parses the JSONEachRow output, and constructs a `QueryResult` exposing `.result_rows`, `.column_names`, and `.named_results()` matching the lib's shape.

The Session is created at construction and disposed in `close()`. One Session per process, bound to `CHDB_DATA_PATH`.

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
    """SQL string literal escaping for row-policy values: 'O''Brien'."""

def policy_name(database: str, table: str, role: str, value: str) -> str:
    """Build a row-policy name: <db>_<table>_<role>_<slug>_<8charhash>.
    The slug is value with non-[a-zA-Z0-9_] stripped; the hash of the raw
    value disambiguates collisions ('EU/UK' vs 'EU UK')."""
```

`validate_identifier` is called at the entry point of every operation function on every external-source string (username, group names, db/table/column). That single guarantee combined with `quote_identifier` for inlining makes the DDL surface safe.

`quote_string` is needed for row-policy values: the user-supplied `value` is embedded into `USING column = '<value>'` and must be SQL-escaped.

## Operations

### `bootstrap.ensure_service_admin(client, settings) -> None`

Idempotent startup routine. In sequence:

1. (chdb only) `CREATE USER IF NOT EXISTS <service_admin_user> IDENTIFIED WITH no_password`. On external, the user is operator-provisioned; bootstrap presumes its existence (it must, because iris connects as it).
2. `CREATE ROLE IF NOT EXISTS <service_admin_role>`.
3. `GRANT <service_admin_role> TO <service_admin_user>` (idempotent in CH).

`service_admin_user` and `service_admin_role` were already validated at `from_env()`, so this routine doesn't re-validate.

### `users.init_user_rights(client, *, username, groups, settings) -> None`

Signature:

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
5. `GRANT IMPERSONATE ON <username> TO <service_admin_user>`. Idempotent; CH treats re-grants as no-ops.

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

Signature:

```python
def add_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    column: str,
    role: str,
    value: str,           # SQL string literal value for the column comparison
    settings: ClickHouseSettings,
) -> None:
```

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

### `policies.revoke_row_policy(client, *, database, table, column, role, value) -> None`

```sql
DROP ROW POLICY IF EXISTS `<policy_name(db, tbl, role, value)>` ON `<database>`.`<table>`
```

The wildcard service-admin policy is *not* dropped — it's a singleton per table and other policies on the same table may still reference it.

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

### Fixtures (`tests/clickhouse/conftest.py`)

```python
@pytest.fixture
def chdb_client(tmp_path):
    settings = make_settings(backend="chdb", chdb_path=tmp_path / "chdb", ...)
    client = build_client(settings)
    ensure_service_admin(client, settings)
    yield client
    client.close()
```

Fresh persistent dir per test → guaranteed isolation. Fixture-scope is per-test by default; can be relaxed to module if startup cost matters.

### Per-module test focus

- `test_settings.py` — env parsing, validation (bad backend, missing vars, bad identifiers, typo'd booleans).
- `test_identifiers.py` — validate / quote / `policy_name` slug+hash behavior, including collision resolution for values that share a slug.
- `test_chdb_smoke.py` — **Phase-0 verification.** Runs each DDL we plan to use (`CREATE USER`, `CREATE ROLE`, `GRANT`, `REVOKE`, `GRANT IMPERSONATE`, `CREATE ROW POLICY`, `DROP ROW POLICY`, `SELECT FROM system.grants/role_grants/row_policies`) against a clean chdb Session. If any fails, the design adapts (likely `access_control_path` config tweak); we want this to fail at the smoke test, not deep in another file.
- `test_bootstrap.py` — service admin role created; granted to user; idempotent on re-run.
- `test_users.py` — full `init_user_rights` flow: creation, group reconcile (start `[a, b]`, reconcile to `[b, c]` → `a_GRP` revoked, `c_GRP` granted, `b_GRP` untouched), IMPERSONATE grant, idempotency.
- `test_grants.py` — both grant functions, idempotency.
- `test_policies.py` — add + revoke; wildcard policy presence after `add_row_policy`; wildcard policy *not* dropped by `revoke_row_policy`.
- `test_audit.py` — every audit function returns the expected rows for a known fixture state.
- `test_external.py` — skipped unless `CLICKHOUSE_TEST_EXTERNAL=1`. When run, exercises `ConnectClient` against a real CH (operator's responsibility to provide). Same assertions as the chdb suite. Useful for catching backend-specific quirks.

### Top-level `tests/conftest.py`

Adds:

```python
os.environ.setdefault("CLICKHOUSE_BACKEND", "chdb")
os.environ.setdefault("CLICKHOUSE_SERVICE_ADMIN_USER", "iris_service")
os.environ.setdefault("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")
os.environ.setdefault("CHDB_DATA_PATH", str(Path(tempfile.gettempdir()) / "iris_chdb_default"))
```

Same pattern as `AUTH_METHOD=mock`. Per-test fixtures override `CHDB_DATA_PATH` via the `tmp_path`-backed `chdb_client` fixture.

## Open verification items

- **chdb access-control surface.** chdb embeds ClickHouse, but the access-control state (`users.xml`, `access_control_path`) needs to be persisted by the Session. Phase-0 smoke test verifies all DDL works. If it doesn't, the fallback is configuring `access_control_path` explicitly via Session settings, or — last resort — switching tests to a Docker-based CH testcontainer behind the same `Client` interface.
- **`{name:Type}` substitution in chdb.** chdb's `Session.query` doesn't accept a parameters dict. We rely on `clickhouse_connect.driver.binding.bind_query_params` (or its public equivalent) to pre-render the SQL client-side. The exact import path needs confirmation against the installed `clickhouse-connect` version during implementation.
- **`GRANT IMPERSONATE` syntax.** The user-provided example reads `GRANT IMPERSONATE ON <user> TO <admin>`. The exact CH syntax (presence/absence of `ON *.*`, etc.) varies by version; the smoke test pins the form for our installed chdb.

## Deliverables (this PR)

- `src/iris/clickhouse/` package as outlined above.
- `tests/clickhouse/` test suite as outlined above.
- `.env` updated with the new section, comments, and example values (no secrets).
- `pyproject.toml` adds `clickhouse-connect` to runtime deps. (`chdb` is already present.)
- `CLAUDE.md` gets a "ClickHouse module" section mirroring the "Authentication" section's style.

## Non-deliverables (explicitly deferred)

- Routes that call any of these functions.
- Any wiring between `iris.auth` and `iris.clickhouse`.
- The runtime `execute_as(username, sql)` helper for impersonating queries.
- Multi-backend live reload, connection pooling, or migration tooling.
