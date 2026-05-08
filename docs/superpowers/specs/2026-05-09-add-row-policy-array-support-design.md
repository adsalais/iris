# add_row_policy: Array(String) column support — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

`iris.clickhouse.policies.add_row_policy` builds a restrictive row policy
of the form `<col> = <quoted_value>`. That clause works for scalar
`String` columns but never matches when `<col>` is an `Array(...)`: in
ClickHouse, `arr_col = 'EU'` compares the entire array to a string and
returns `0` for every row.

The natural CH idiom for "row is allowed if the value appears anywhere
in the array" is `has(<col>, <val>)`. The fix is to detect array
columns and emit the `has(...)` form instead of `=`.

## Goals

- `add_row_policy` automatically generates the right USING clause for
  the column it targets: `=` for scalars, `has(...)` for `Array(...)`.
- Caller signature unchanged.
- The semantic outcome (a non-admin role only sees rows where the value
  is contained in / equals the column) is verified end-to-end against
  the CH testcontainer.
- Loud failure when the column doesn't exist or when the array element
  type isn't supported — silent malformed policies are the bug we're
  trying to avoid.

## Non-goals

- `Array(Int*)`, `Array(Float*)`, `Array(DateTime*)`, `Array(Date*)`,
  `Array(Bool)`. These would require typed-value handling like the
  CH HTTP-param marshaller. Defer until iris uses them.
- `Map(K, V)`, `Tuple(...)`, `Array(Array(...))`. Out of scope.
- Multi-element matching (`hasAny(col, [v1, v2])`). The current API
  takes a single value; that stays.
- Touching `revoke_row_policy`. Row-policy names are derived from
  `(database, table, role, value)`; CH drops by name, not by USING
  clause. The revoke path needs no change.

---

## Architecture

### Detection

`add_row_policy` queries `system.columns` once per call to determine
the column's CH type:

```python
def _column_type(
    client: Client, *, database: str, table: str, column: str
) -> str:
    """Return the CH type of <database>.<table>.<column>; raise if missing."""
    rows = client.query(
        "SELECT type FROM system.columns "
        "WHERE database = {d:String} AND table = {t:String} AND name = {c:String}",
        parameters={"d": database, "t": table, "c": column},
    ).result_rows
    if not rows:
        raise ValueError(
            f"column {database}.{table}.{column} does not exist"
        )
    return cast(str, rows[0][0])
```

The query goes through clickhouse-connect's binary protocol (the
`Client` already in scope), which auto-marshals the parameters
correctly.

### USING-clause builder

The clause builder peels one optional `Nullable(...)` wrap and accepts
only `String` or `FixedString(N)` as the inner element type:

```python
_FIXED_STRING_RE = re.compile(r"^FixedString\(\d+\)$")


def _build_policy_filter(col_q: str, col_type: str, value: str) -> str:
    """Build the USING clause; uses has(...) for supported Array(String) variants."""
    if col_type.startswith("Array(") and col_type.endswith(")"):
        inner = col_type[len("Array(") : -1].strip()
        if inner.startswith("Nullable(") and inner.endswith(")"):
            inner = inner[len("Nullable(") : -1].strip()
        if inner != "String" and not _FIXED_STRING_RE.match(inner):
            raise TypeError(
                f"add_row_policy supports Array(String) variants only; "
                f"got {col_type}. Extend add_row_policy or pass non-array "
                f"columns directly."
            )
        return f"has({col_q}, {quote_string(value)})"
    return f"{col_q} = {quote_string(value)}"
```

### Wire-up in `add_row_policy`

The hardcoded `f"FOR SELECT USING {column_q} = {quote_string(value)} TO {role_q}"`
becomes:

```python
col_type = _column_type(client, database=database, table=table, column=column)
clause = _build_policy_filter(column_q, col_type, value)
client.command(
    " ".join((
        f"CREATE ROW POLICY IF NOT EXISTS {name_q} ON {db_q}.{table_q}",
        f"FOR SELECT USING {clause} TO {role_q}",
    ))
)
```

The two `USING 1` wildcard policies (for `iris_global_admin` and
`<db>_DBADMIN`) are unaffected — they don't reference the column.

### What changes about the docstring

`add_row_policy`'s docstring already covers wildcard policies; add one
short paragraph stating that `Array(String)` columns get a `has(...)`
clause and that other element types raise.

---

## Files touched

| File | Change |
|---|---|
| `src/iris/clickhouse/policies.py` | Add `_column_type` and `_build_policy_filter` helpers + `_FIXED_STRING_RE` constant + `import re` and `from typing import cast` (or extend the existing imports). Modify `add_row_policy` to dispatch via the helpers. Extend the docstring. |
| `tests/clickhouse/test_clickhouse_policies.py` | Add 7 new tests covering scalar-still-works, the four array-acceptance paths, and two error paths. |

---

## Test plan

### Setup helpers

The existing `_setup_table` in `test_clickhouse_policies.py` only
covers the scalar-`region` shape. Add a second helper that takes a
column type so each new test can declare its own table shape:

```python
def _setup_typed_table(
    ch_client, db: str, table: str, role: str, column: str, column_type: str
) -> None:
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` "
        f"(id UInt64, `{column}` {column_type}) "
        f"ENGINE = MergeTree ORDER BY id"
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    dba_role = tier_role_name(db, TIER_DBADMIN)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{dba_role}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")
```

### Tests

1. **`test_add_row_policy_string_column_uses_equals`**
   Existing path regression: scalar `String` column, assert the
   `select_filter` from `system.row_policies` contains `=` and not
   `has(`.

2. **`test_add_row_policy_array_string_uses_has`**
   `tags Array(String)`, call with `value='EU'`, assert `select_filter`
   starts with `has(` and contains `'EU'`.

3. **`test_add_row_policy_array_string_filter_works_end_to_end`**
   Table with `tags Array(String)`. Insert rows
   `(1, ['EU','UK'])` and `(2, ['US'])`. Create a CH user, grant the
   policy's role to its `<username>_USER` per-user role (matches the
   tier-grant pattern), grant `SELECT ON <db>.<table>` to that role,
   then query as that user via `query_as_user(... 'SELECT id FROM <table> ORDER BY id')`
   and assert the result is `[{"id": 1}]`.

4. **`test_add_row_policy_nullable_array_string_uses_has`**
   `tags Array(Nullable(String))`, assert `has(`.

5. **`test_add_row_policy_array_fixed_string_uses_has`**
   `tags Array(FixedString(8))`, value `'eu      '` (padded to 8), assert
   `has(`. The padding mirrors how CH stores `FixedString(8)` literals.

6. **`test_add_row_policy_array_int_raises`**
   `nums Array(Int32)`, assert `TypeError` matching `"Array(Int32)"`.

7. **`test_add_row_policy_unknown_column_raises`**
   Column name doesn't exist on the table, assert `ValueError` matching
   `"does not exist"`.

The end-to-end test (#3) is the load-bearing one: it verifies the
USING clause actually filters in CH, not just that the SQL string
contains `has(`.

---

## Risks and edge cases

- **`system.columns` privilege.** iris's service identity has `SELECT`
  on `system.columns` (conftest grants explicitly; production
  `bootstrap_admin` does `GRANT ALL ON *.*`). No new privilege required.
- **One extra read query per call.** `add_row_policy` is admin-rare
  (config-time, not request-path). Latency cost is in the milliseconds.
  No caching needed.
- **Inner-type peel is naive.** String slicing on `Array(...)` /
  `Nullable(...)`. Sufficient for the four supported shapes; correctly
  rejects `Map(...)`, `Tuple(...)`, `Array(Array(...))` because the
  inner doesn't equal `String` and doesn't match `_FIXED_STRING_RE`.
- **`FixedString(N)` value padding is the caller's responsibility.**
  CH stores `FixedString(8)` values right-padded with NUL bytes; a
  caller passing `'EU'` against `Array(FixedString(8))` will get a
  policy that never matches. The marshaller can't fix this from the
  outside; document the gotcha in the docstring.
- **Race between schema lookup and policy creation.** A concurrent
  `ALTER TABLE` could change the column type between our
  `system.columns` read and the `CREATE ROW POLICY`. The window is
  small and the failure mode is a malformed policy that CH rejects at
  creation time — acceptable for this admin-rare path.

---

## Out of scope (deferred)

- Numeric / date / boolean array element types. Track if a future
  caller needs `Array(Int*)` or `Array(DateTime)`; reuse the marshaller
  shape from `iris.clickhouse.queries` when adding.
- `hasAny(col, [v1, v2, ...])` for multi-value policies. Today's API
  is one-policy-per-value; multi-value would either change the API or
  require multiple policy rows per call.
- Caching the column-type lookup. The cost is too low to justify the
  cache-invalidation complexity.
