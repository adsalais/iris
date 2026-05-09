# Add `add_row_dict_policy` — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

Iris currently exposes one row-policy primitive: `add_row_policy` (in `iris.clickhouse.policies`) builds a USING clause of the form `<column> = <value>` (scalar) or `has(<column>, <value>)` (Array(String) variants). Both are static — the policy directly compares a row's column value to the policy-baked literal.

Real-world authz often requires a level of indirection: each row carries an identifier (e.g. `auth_id`) into a side table that lists the *tags* allowed to see that row. The mapping changes frequently (operator-managed); the policy itself rarely changes.

ClickHouse's `dictGet` enables exactly this pattern in a USING clause:

```sql
CREATE ROW POLICY rp_eu_team ON test_auth FOR SELECT
  USING has(dictGet('default.authorisations_dict', 'authorisations', auth_id), 'eu_team')
  TO bob_role;
```

The experiment in §1 proves CH 26.3 evaluates this correctly: the dict is consulted per row, the row's `auth_id` column value is the dict key, and the policy-static `'eu_team'` is checked for membership in the returned `Array(String)`.

This spec adds an iris helper `add_row_dict_policy` (and its `revoke_row_dict_policy` mate, plus matching `DatabaseAdminSession` wrappers) that emits this clause shape with the same SQL-hygiene, naming, idempotency, and admin-wildcard discipline as the existing scalar `add_row_policy`.

## Goal

Programmatic creation and revocation of dict-keyed row policies on user databases, validated end-to-end against a real ClickHouse 26.3, with the identifier-validation, wildcard-preservation, and idempotency invariants of the existing scalar helpers.

## Non-goals

- **Dictionary or source-table provisioning.** Iris does not create the side table (e.g. `authorisations`) or the dictionary (e.g. `authorisations_dict`); those are app-level data the operator manages externally. Iris only creates the row policy that references them.
- **UI integration.** The manage-database row-policies section in `iris.features.authorization` continues to expose only the scalar helper. Adding a "use dict policy" toggle to that form is a follow-up spec.
- **Other `dictGet` variants** (`dictGetOrDefault`, `dictGetOrNull`, `dictHas`). The experiment validated `dictGet` with `Array(String)` attributes only. Other variants can be added when a concrete need surfaces.
- **Non-`Array(String)` attribute types.** The dict's authorisations attribute must be `Array(String)` (or its `Nullable` / `FixedString` variants — same set the existing `add_row_policy` already supports for direct array columns). Other element types raise `TypeError` at the call site, mirroring the scalar helper's behavior.
- **Cross-database dict keys.** The dict source table can live in any database (the dict's `SOURCE` clause handles it); the policy references the dict by `<db>.<dict>` form (§5.2).

## 1. Experiment summary

Validated against `clickhouse/clickhouse-server:26.3` on 2026-05-09:

```sql
CREATE TABLE authorisations (key String, authorisations Array(String))
  ENGINE = MergeTree ORDER BY key;
INSERT INTO authorisations VALUES
  ('rec-1', ['public']),
  ('rec-2', ['internal', 'eu_team']),
  ('rec-3', ['eu_team']),
  ('rec-4', ['secret']);

CREATE DICTIONARY authorisations_dict (key String, authorisations Array(String))
  PRIMARY KEY key
  SOURCE(CLICKHOUSE(TABLE 'authorisations'))
  LAYOUT(COMPLEX_KEY_HASHED())
  LIFETIME(MIN 600 MAX 900);

CREATE TABLE test_auth (id UInt32, region String, auth_id String)
  ENGINE = MergeTree ORDER BY id;
INSERT INTO test_auth VALUES
  (10, 'eu', 'rec-1'), (20, 'eu', 'rec-2'),
  (30, 'eu', 'rec-3'), (40, 'eu', 'rec-4');

GRANT SELECT ON test_auth TO alice_role, bob_role, eve_role;
GRANT dictGet ON authorisations_dict TO alice_role, bob_role, eve_role;

CREATE ROW POLICY rp_public  ON test_auth FOR SELECT
  USING has(dictGet('default.authorisations_dict', 'authorisations', auth_id), 'public')
  TO alice_role;
CREATE ROW POLICY rp_eu_team ON test_auth FOR SELECT
  USING has(dictGet('default.authorisations_dict', 'authorisations', auth_id), 'eu_team')
  TO bob_role;
CREATE ROW POLICY rp_secret  ON test_auth FOR SELECT
  USING has(dictGet('default.authorisations_dict', 'authorisations', auth_id), 'secret')
  TO eve_role;
```

Results:

| User  | Role            | Policy            | Visible rows                             |
|-------|-----------------|-------------------|------------------------------------------|
| alice | `alice_role`    | `rp_public`       | id=10 (rec-1 has 'public')               |
| bob   | `bob_role`      | `rp_eu_team`      | id=20, 30 (rec-2 and rec-3 have 'eu_team') |
| eve   | `eve_role`      | `rp_secret`       | id=40 (rec-4 has 'secret')               |

Caveat surfaced by the experiment: callers granting the role must also `GRANT dictGet ON <dictionary> TO <role>` so the policy can evaluate. This is **out of scope for `add_row_dict_policy`** — the helper assumes the operator/admin has already granted `dictGet` on the dict to whichever roles will be subject to the policy. The spec calls it out explicitly so consumers are not surprised.

## 2. API surface

### 2.1 Module-level helpers (`iris.clickhouse.policies`)

```python
def add_row_dict_policy(
    client: Client,
    *,
    database: str,        # protected table's database
    table: str,           # protected table
    auth_id: str,         # column on the protected table; used as the dict key
    dictionary: str,      # dict name; "name" (current db) or "db.name"
    authorisations: str,  # dict attribute; must be Array(String) (or its
                          # Nullable / FixedString variants)
    role: str,            # role to attach the policy to
    value: str,           # static value; the dict's array must contain this
                          # for the row to be visible to <role>
) -> None: ...


def revoke_row_dict_policy(
    client: Client,
    *,
    database: str,
    table: str,
    auth_id: str,
    dictionary: str,
    authorisations: str,
    role: str,
    value: str,
) -> None: ...
```

Both signatures take all seven dict-policy params so the deterministic name (§4) is reproducible at revoke time. Mirrors the scalar `add_row_policy` / `revoke_row_policy` shape.

### 2.2 `DatabaseAdminSession` wrappers (`iris.auth.views`)

```python
async def add_row_dict_policy(
    self, *,
    table: str,
    auth_id: str,
    dictionary: str,
    authorisations: str,
    role: str,
    value: str,
) -> None: ...

async def revoke_row_dict_policy(
    self, *,
    table: str,
    auth_id: str,
    dictionary: str,
    authorisations: str,
    role: str,
    value: str,
) -> None: ...
```

`database` is auto-scoped to `self.database` (the existing wrapper convention). Bodies follow the existing `add_row_policy` wrapper exactly: `client, _, _ = self._ch(); await asyncio.to_thread(policies.add_row_dict_policy, client, database=self.database, …)`.

## 3. Identifier validation and quoting

| Param            | Validation                                         | Emitted as in SQL                  |
|------------------|----------------------------------------------------|------------------------------------|
| `database`       | `validate_identifier(kind="database")`             | backtick-quoted identifier         |
| `table`          | `validate_identifier(kind="table")`                | backtick-quoted identifier         |
| `auth_id`        | `validate_identifier(kind="column")`               | backtick-quoted identifier         |
| `role`           | `validate_identifier(kind="role")`                 | backtick-quoted identifier         |
| `dictionary`     | split on `.` (max one); validate each part         | single-quoted SQL string literal   |
|                  | as `kind="database"` (left) and `kind="table"` (right) | (e.g. `'iris_dicts.auth_dict'`) |
| `authorisations` | `validate_identifier(kind="column")`               | single-quoted SQL string literal   |
| `value`          | (none — opaque)                                    | `quote_sql_literal(value)`         |

Identifier validation rejects anything outside `[a-zA-Z0-9_]+`, raising `InvalidIdentifierError`. The `dictionary` split rule allows the common `db.dict` form while preventing injection via crafted dict names; both halves go through `validate_identifier` before being concatenated back into the string literal.

Defense in depth: `dictionary` and `authorisations` are emitted as string literals (so SQL injection via these would require breaking out of single quotes — `quote_sql_literal` handles the escaping), AND validated as identifiers (so iris rejects obvious garbage like `dictionary="; DROP TABLE …"` early at the API surface, before any SQL is built).

## 4. Naming + idempotency

A new helper alongside the existing `policy_name`:

```python
def dict_policy_name(
    database: str, table: str, role: str, value: str,
    dictionary: str, authorisations: str, auth_id: str,
) -> str:
    """Same shape as policy_name but the hash incorporates the dict params.

    Format: ``<db>_<table>_<role>_<slug>_<16charhash>``
    Hash input: ``value | dictionary | authorisations | auth_id`` (joined
    with a separator that can't appear in any of the parts, e.g. NUL).
    """
```

The 16-char SHA-256 prefix protects against collisions for the same `(database, table, role)` triple where two dict policies use distinct `(dictionary, authorisations, auth_id, value)` combinations. The slug component (derived from `value` only, like the scalar `policy_name`) keeps the name humanly recognisable.

Idempotency: `add_row_dict_policy` issues `CREATE ROW POLICY IF NOT EXISTS <dict_policy_name> ON <db>.<table> FOR SELECT USING <clause> TO <role>`. Re-running with the same args is a no-op. Re-running with any param changed produces a NEW policy name (different hash), so previous instances coexist until explicitly revoked — same trade-off as the scalar version.

## 5. SQL produced

### 5.1 The restrictive policy

```sql
CREATE ROW POLICY IF NOT EXISTS `<dict_policy_name>`
  ON `<database>`.`<table>`
  FOR SELECT
  USING has(dictGet('<dictionary>', '<authorisations>', `<auth_id>`), <value_quoted>)
  TO `<role>`
```

`<value_quoted>` is `quote_sql_literal(value)` (single-quoted, escaped).

### 5.2 The two admin wildcards (mirror existing `add_row_policy`)

Both are emitted with deterministic names so re-runs are no-ops via `CREATE ROW POLICY IF NOT EXISTS`. They are NOT touched by `revoke_row_dict_policy` — they intentionally persist after the last restrictive policy is removed (existing rule).

```sql
CREATE ROW POLICY IF NOT EXISTS `<database>_<table>_iris_global_admin`
  ON `<database>`.`<table>`
  FOR SELECT USING 1
  TO `iris_global_admin`;

CREATE ROW POLICY IF NOT EXISTS `<database>_<table>_<database>_DBADMIN`
  ON `<database>`.`<table>`
  FOR SELECT USING 1
  TO `<database>_DBADMIN`;
```

If the scalar `add_row_policy` has already created these wildcards on the table, the IF NOT EXISTS makes the dict-policy call a no-op for those two statements. If the dict-policy is the first policy on the table, it seeds them.

### 5.3 Revocation

```sql
DROP ROW POLICY IF EXISTS `<dict_policy_name>` ON `<database>`.`<table>`
```

Wildcards untouched (same rule as scalar revoke). Idempotent.

## 6. Operator responsibilities (out of scope for the helper)

The helper assumes the operator has already done the following before calling it; failure to do so produces a working `CREATE ROW POLICY` but a non-functional policy:

1. **Created the dict source table** with at least the key column and an `Array(String)` attribute column.
2. **Created the dictionary** with the matching layout (`COMPLEX_KEY_HASHED` for `String` keys, `HASHED` for `UInt64`, etc.) and a refresh `LIFETIME` appropriate for how often the underlying data changes. (`SYSTEM RELOAD DICTIONARY <name>` if you need an immediate refresh after an INSERT.)
3. **Granted `dictGet ON <dictionary>`** to every role that the policy will be attached to. Without this grant, the per-row evaluation raises `Code: 497. DB::Exception: <user>: Not enough privileges` and the user sees zero rows from the policy's perspective. (CH treats the missing privilege as "policy did not match" rather than a hard error to the client; it surfaces in the CH server log.)

These are documented in the helper's docstring AND surfaced in `CLAUDE.md` under a new "Operator follow-ups" subsection (added by this work) so future agents and operators can see them at a glance — including the open follow-up to surface a "missing dictGet grant" check in the admin UI when one becomes useful. Iris itself does NOT issue any of these statements.

## 7. Testing

### 7.1 Unit tests (`tests/clickhouse/test_clickhouse_dict_policies.py`)

Mirror the structure of the existing `test_clickhouse_policies.py`:

- `test_add_row_dict_policy_creates_named_policy_and_two_wildcards` — set up a dict + protected table, call helper, assert `system.row_policies` has the named policy plus the two wildcards.
- `test_add_row_dict_policy_is_idempotent` — call twice with same args, assert no error and policy count unchanged.
- `test_add_row_dict_policy_wildcards_no_op_when_scalar_already_present` — call the scalar `add_row_policy` first (which seeds the two wildcards), then `add_row_dict_policy` on the same table; assert the two wildcards still exist exactly once each (i.e. the dict helper's `CREATE ROW POLICY IF NOT EXISTS` for the wildcards was a no-op, NOT a duplicate or replacement). Locks in the §5.2 contract.
- `test_add_row_dict_policy_validates_inputs` — bad database / table / auth_id / role / dictionary / authorisations all raise `InvalidIdentifierError`. Two-dot dictionary (`db.foo.bar`) raises.
- `test_revoke_row_dict_policy_drops_named_policy` — create then revoke, assert the named policy is gone and the wildcards are still present.
- `test_revoke_row_dict_policy_does_not_drop_wildcards` — explicit assertion mirroring the scalar test.
- `test_revoke_row_dict_policy_is_idempotent`.
- `test_dict_policy_name_distinct_for_different_dictionaries` — same `(db, table, role, value)` with different `dictionary` values produce different names (hash differs).

### 7.2 Integration test (real CH testcontainer)

One end-to-end test that mirrors the experiment in §1: create the source table + dict + protected table + role + user, call `add_row_dict_policy`, query as the user, assert the user sees only the rows whose `auth_id` maps to a dict array containing the policy's value. Lives under `tests/clickhouse/integration/test_dict_policy_filters.py` so it runs only when the testcontainer suite is enabled.

### 7.3 `DatabaseAdminSession` wrappers

Two unit tests in `tests/auth/test_database_admin_dict_policies.py` (matching the existing `test_database_admin_row_policies.py` pattern from earlier in this branch): monkeypatch `iris.auth.views.policies.add_row_dict_policy` / `…revoke_row_dict_policy`, instantiate a `DatabaseAdminSession` with mocks, await the wrapper, assert the underlying helper was called with `database=self.database` and the rest of the kwargs forwarded.

## 8. Files

| Path | Change |
|---|---|
| `src/iris/clickhouse/identifiers.py` | Add `dict_policy_name(...)` next to `policy_name`. |
| `src/iris/clickhouse/policies.py` | Add `add_row_dict_policy` + `revoke_row_dict_policy` + a `_build_dict_policy_filter` helper for the USING clause. |
| `src/iris/auth/views.py` | Add `add_row_dict_policy` + `revoke_row_dict_policy` async wrappers on `DatabaseAdminSession`. |
| `tests/clickhouse/test_clickhouse_dict_policies.py` | Unit tests (§7.1). |
| `tests/clickhouse/integration/test_dict_policy_filters.py` | Integration test (§7.2). |
| `tests/auth/test_database_admin_dict_policies.py` | Wrapper tests (§7.3). |
| `CLAUDE.md` | Add "Operator follow-ups" subsection under "Conventions" (or extend the existing follow-up tail) listing the three operator responsibilities from §6 plus the open admin-UI follow-up. |

## 9. Risks and tradeoffs

- **Per-row dict lookup performance.** With `COMPLEX_KEY_HASHED`/`HASHED` layouts the dict is in memory and lookups are O(1); CH's dict-aware planner can also reorder evaluation. For large tables (>10M rows) the policy adds noticeable per-query CPU but no I/O. The operator chooses dict layout and `LIFETIME` to fit their workload.
- **Stale dict data.** `LIFETIME(MIN 600 MAX 900)` (the example from §3) means up-to-15-minute lag between an authorisations-table update and the policy seeing it. Operators with hard real-time requirements should use `LIFETIME(MIN 0 MAX 0)` and `SYSTEM RELOAD DICTIONARY <name>` after each update, or use a CH-engine dict directly (no caching). Iris doesn't pick the layout; the operator does.
- **Missing `GRANT dictGet`.** A common operator mistake (item 3 in §6). The policy silently filters everything for affected roles. Documented in the helper's docstring; consider surfacing in the future admin UI as a check ("role X holds `<db>.<table>` SELECT but lacks dictGet on referenced `<dict>`").
- **Two-dot dictionary names.** CH technically accepts `db.dict` with no further nesting; the spec rejects three-component names like `db.schema.dict`. Acceptable for MVP — CH doesn't have schemas, so two-component is the natural maximum.
