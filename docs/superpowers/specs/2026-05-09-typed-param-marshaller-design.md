# Typed CH parameter marshaller — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

Commit `52fab63` added a private `_marshal_param(v)` helper in
`iris.clickhouse.queries` that maps Python values to the CH HTTP
``param_<name>`` query string. It dispatched on the Python *value type*
only: ``bool`` → ``"1"/"0"``, ``int|float|str`` → ``str(v)``,
``datetime`` → ISO with second precision and the UTC suffix stripped.

That's not enough. CH placeholders carry the target type (``{name:Type}``);
the marshaller has to honor it. Concrete failures of the current code:

- ``DateTime64(3)`` requires fractional seconds (``…12:34:56.789``); the
  current ``timespec="seconds"`` truncates them silently.
- ``Array(String)`` and ``Nullable(...)`` aren't supported at all.
- A naive ``str(v)`` for a Python ``datetime`` of ``DateTime`` type happens
  to work but only because CH is permissive on ISO format. Add an offset
  (``…+02:00``) and CH rejects it.

The fix: parse the SQL for ``{name:Type}`` placeholders, build a
``name → type_str`` map, and dispatch the marshaller on the declared
type rather than the Python value's type.

## Goals

- Replace the value-typed ``_marshal_param(v)`` with a type-driven
  ``_marshal_param(v, ch_type)`` that produces the wire format CH expects
  for that placeholder type.
- Support every type the iris codebase plausibly needs: scalars, the
  full ``DateTime`` family (including precision-aware ``DateTime64(p)``),
  ``Array(T)``, and ``Nullable(T)``.
- Surface caller mistakes early: parameters that don't bind to any
  placeholder, and SQL with conflicting type declarations for the same
  name.
- Cover the marshaller with unit tests and a CH-roundtrip integration
  test that proves the wire format is what CH actually parses.

## Non-goals

- Decimal, UUID, Map, Tuple, Tuple-of-Tuples, LowCardinality, Enum.
  Add when iris uses them.
- Caller-supplied type wrappers (``Param(value, "DateTime64(3)")``).
  Revisit only if SQL parsing turns out insufficient.
- Touching the ``query_as_service`` path. That goes through
  clickhouse-connect's binary protocol, which marshals natively.

---

## Architecture

### Public-facing change

```python
# Before
async def query_as_user(http_client, *, username, sql, parameters=None, database=None):
    ...
    if parameters:
        for k, v in parameters.items():
            params[f"param_{k}"] = _marshal_param(v)
```

```python
# After
async def query_as_user(http_client, *, username, sql, parameters=None, database=None):
    ...
    if parameters:
        type_map = _parse_placeholder_types(sql)
        for k, v in parameters.items():
            if k not in type_map:
                raise ValueError(
                    f"parameter {k!r} has no {{{k}:Type}} placeholder in the SQL"
                )
            params[f"param_{k}"] = _marshal_param(v, type_map[k])
```

### Helper 1 — `_parse_placeholder_types`

```python
_PLACEHOLDER_RE = re.compile(r"\{(\w+):([^}]+)\}")


def _parse_placeholder_types(sql: str) -> dict[str, str]:
    """Extract every ``{name:Type}`` from ``sql`` into a name → type-string map.

    Raises ``ValueError`` if the same name appears with conflicting types.
    Whitespace inside the type string is preserved (CH accepts e.g.
    ``Decimal(10, 2)`` with internal spaces); the type string is trimmed
    of leading/trailing whitespace only.
    """
    found: dict[str, str] = {}
    for m in _PLACEHOLDER_RE.finditer(sql):
        name, ch_type = m.group(1), m.group(2).strip()
        prev = found.get(name)
        if prev is not None and prev != ch_type:
            raise ValueError(
                f"conflicting CH types for placeholder {name!r}: "
                f"{prev!r} vs {ch_type!r}"
            )
        found[name] = ch_type
    return found
```

The regex `\{(\w+):([^}]+)\}` works because CH type strings never
contain `}` — even nested types like ``Array(Nullable(Int32))`` stay
within parens.

### Helper 2 — `_marshal_param(v, ch_type)`

Strips whitespace from `ch_type`, peels off `Nullable(...)` and
`Array(...)` wrappers (recursive), then dispatches the inner type to a
leaf handler. Dispatch table:

| CH type pattern | Python value | Wire format |
|---|---|---|
| `String`, `FixedString(N)` | `str` | as-is |
| `Int8`, `Int16`, `Int32`, `Int64`, `UInt8`, `UInt16`, `UInt32`, `UInt64` | `int` (rejects `bool`) | `str(v)` |
| `Float32`, `Float64` | `int` or `float` (rejects `bool`) | `str(v)` |
| `Bool` | `bool` | `"true"` / `"false"` |
| `Date`, `Date32` | `date` or `datetime` | `YYYY-MM-DD` |
| `DateTime`, `DateTime('<TZ>')` | `datetime` (naive treated as UTC) | `YYYY-MM-DD HH:MM:SS` in UTC |
| `DateTime64(p)` | `datetime` (naive treated as UTC) | `YYYY-MM-DD HH:MM:SS.fff…` with `p` digits in UTC |
| `Array(T)` | `list` or `tuple` | `[m(e1),m(e2),…]` recursive; string elements auto-quoted with `'…'` |
| `Nullable(T)` | `None` or `T`-shaped | `NULL` literal or `m(v, T)` |

Any other type string raises
``TypeError(f"unsupported CH param type: {ch_type!r}")``.

### Datetime semantics

- A naive ``datetime`` (no `tzinfo`) is treated as UTC. Iris already
  uses `datetime.now(UTC)` everywhere; this matches the convention.
- An aware ``datetime`` is converted to UTC before formatting.
- ``DateTime`` always renders with second precision; sub-second
  components are truncated.
- ``DateTime64(p)`` renders with exactly `p` fractional digits.
  Python's ``microsecond`` field has 6 digits of precision; for `p < 6`
  we truncate (toward zero), for `p > 6` we right-pad zeros. (CH
  supports `p` up to 9 — nanosecond precision — but Python's stdlib
  caps at 6, so values past that are zero.)

### `Array(T)` formatting

CH's HTTP param parser accepts the literal-array syntax
``[v1, v2, …]``. Each element is marshaled per `T` recursively. Strings
inside arrays must be single-quoted: ``['a','b']``. Backslashes and
single quotes in string elements are escaped: ``'O\\'Brien'``,
``'with\\\\backslash'``. Top-level strings (`String` outside an array)
do *not* get quoted by the marshaller — CH wraps them automatically
when the param is parsed as `String`.

### `Nullable(T)` formatting

``None`` → the literal three-letter string ``NULL``. CH's HTTP param
parser interprets the bare token `NULL` as a SQL null when the
declared type is `Nullable(T)`. Non-`None` values fall through to the
inner-type handler.

### Number-type integer rejection of `bool`

Python's `bool` is a subclass of `int`; the leaf handler for `Int*`
and `UInt*` must check `isinstance(v, bool)` *first* and reject. The
current source already encodes this for the ``Bool``→``"1"/"0"``
case; we keep it and extend to the integer branches so that the
opposite mistake (passing `True` for `Int32`) fails loudly instead of
producing the literal string ``"True"``.

---

## Files touched

| File | Change |
|---|---|
| `src/iris/clickhouse/queries.py` | Replace `_marshal_param(v)` with `_marshal_param(v, ch_type)`. Add `_parse_placeholder_types(sql)`. Update `query_as_user` to wire both. |
| `tests/clickhouse/test_query_marshaling.py` | Rewrite the existing 8 unit tests to pass `ch_type`. Add coverage for placeholder parsing, every leaf type, `Array(T)`/`Nullable(T)` recursion, and DateTime/DateTime64 precision/timezone edges. |
| `tests/clickhouse/test_query_marshaling_integration.py` *(new)* | CH-roundtrip integration test that builds a real table covering all supported types, inserts known values, runs per-type parameterized SELECTs, and asserts CH returns the expected rows. |

---

## Test plan

### Unit tests (`tests/clickhouse/test_query_marshaling.py`)

Pure-Python coverage — no testcontainer.

**Placeholder parser:**
- Basic: ``"WHERE x = {x:Int32}"`` → ``{"x": "Int32"}``.
- Nested type: ``"WHERE xs = {xs:Array(Nullable(Int32))}"`` → ``{"xs": "Array(Nullable(Int32))"}``.
- Multiple distinct placeholders.
- Same name twice with same type — accepted, single entry.
- Same name twice with conflicting types — raises ``ValueError``.
- No placeholders — empty dict.
- Whitespace in type — preserved trimmed.

**Leaf-type marshaling (each as a parametrized test or named pair):**

- `String` — returns the value verbatim, including UTF-8 and embedded quotes.
- `Int*` / `UInt*` — `42 → "42"`, `-1 → "-1"`. `True → TypeError`.
- `Float64` — `3.14 → "3.14"`, `42 → "42"` (int passthrough). `True → TypeError`.
- `Bool` — `True → "true"`, `False → "false"`.
- `Date`, `Date32` — both `date(2026,5,9)` and `datetime(2026,5,9,…)` render as `"2026-05-09"`.
- `DateTime` — naive `datetime(2026,5,9,12,0,0)` and aware UTC equivalent both render `"2026-05-09 12:00:00"`. Aware non-UTC renders the UTC equivalent.
- `DateTime('UTC')` — same handler.
- `DateTime64(3)` — `datetime(2026,5,9,12,34,56,789000,tzinfo=UTC) → "2026-05-09 12:34:56.789"`.
- `DateTime64(6)` — full microsecond preserved.
- `DateTime64(0)` — fractional truncated entirely.
- `Array(String)` — `["a","b"] → "['a','b']"`. Embedded quotes escaped: `["O'Brien"] → "['O\\'Brien']"`.
- `Array(Int32)` — `[1,2,3] → "[1,2,3]"`.
- `Nullable(String)` — `None → "NULL"`, `"x" → "x"`.
- `Array(Nullable(Int32))` — `[1, None, 2] → "[1,NULL,2]"`.

**Strictness:**
- `query_as_user` raises when a parameter key has no matching `{name:Type}` placeholder in SQL.
- Test the error message names the offending key.

### Integration tests (`tests/clickhouse/test_query_marshaling_integration.py`)

Uses `ch_client` (testcontainer) and `prefix` fixtures from
`tests/clickhouse/conftest.py`.

**Setup**

```sql
CREATE TABLE `<prefix>_marshal_check` (
    id      Int32,
    s       String,
    fs      FixedString(8),
    u8      UInt8,
    i32     Int32,
    u64     UInt64,
    f64     Float64,
    b       Bool,
    d       Date,
    dt      DateTime,
    dt_tz   DateTime('UTC'),
    dt64_3  DateTime64(3),
    arr_s   Array(String),
    arr_i   Array(Int32),
    n_s     Nullable(String),
    arr_n_i Array(Nullable(Int32))
) ENGINE = Memory
```

Insert exactly two rows via `ch_client.command(INSERT VALUES …)`:

- Row 1 (id=1): `s="hello"`, `fs="abcdefgh"`, `u8=200`, `i32=-7`,
  `u64=18446744073709551615`, `f64=3.14`, `b=true`,
  `d='2026-05-09'`, `dt='2026-05-09 12:00:00'`,
  `dt_tz='2026-05-09 12:00:00'`, `dt64_3='2026-05-09 12:34:56.789'`,
  `arr_s=['alice','bob']`, `arr_i=[1,2,3]`, `n_s='filled'`,
  `arr_n_i=[1,NULL,3]`.
- Row 2 (id=2): `n_s` is NULL, everything else mirrors row 1's
  populated baseline (so we can WHERE-match without distinguishing the
  rows by other columns).

**Assertions** — for each supported type, build a query through the
HTTP endpoint exactly the way `_marshal_param` is exercised:

```python
async def _query_as_admin(
    http: httpx.AsyncClient, sql: str, parameters: dict[str, object]
) -> list[dict]:
    """Bypass EXECUTE AS — exercises only the marshaller pipeline.

    Mirrors query_as_user without the impersonation prefix, so we don't
    need a separate test user with IMPERSONATE granted.
    """
    type_map = _parse_placeholder_types(sql)
    params = {"default_format": "JSONEachRow"}
    for k, v in parameters.items():
        params[f"param_{k}"] = _marshal_param(v, type_map[k])
    r = await http.post("/", params=params, content=sql)
    r.raise_for_status()
    text = r.text.strip()
    return [json.loads(line) for line in text.splitlines() if line]
```

Per-type test (parametrized with `pytest.mark.parametrize`):

| Column | Type | Python value | Asserts |
|---|---|---|---|
| s | `String` | `"hello"` | row 1 returned |
| fs | `FixedString(8)` | `"abcdefgh"` | row 1 returned |
| u8 | `UInt8` | `200` | row 1 returned |
| i32 | `Int32` | `-7` | row 1 returned |
| u64 | `UInt64` | `18446744073709551615` | row 1 returned |
| f64 | `Float64` | `3.14` | row 1 returned |
| b | `Bool` | `True` | row 1 returned |
| d | `Date` | `date(2026, 5, 9)` | row 1 returned |
| dt | `DateTime` | `datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)` | row 1 returned |
| dt_tz | `DateTime('UTC')` | same datetime | row 1 returned |
| **dt64_3** | **`DateTime64(3)`** | **`datetime(2026,5,9,12,34,56,789000,tzinfo=UTC)`** | **row 1 returned (proves millisecond fidelity)** |
| arr_s | `Array(String)` | `["alice","bob"]` | row 1 returned |
| arr_i | `Array(Int32)` | `[1,2,3]` | row 1 returned |
| n_s | `Nullable(String)` | `"filled"` | row 1 returned |
| arr_n_i | `Array(Nullable(Int32))` | `[1, None, 3]` | row 1 returned |

Plus one Nullable-NULL assertion via `WHERE n_s IS NULL` returning row 2,
and one explicit failure case: `Array(Int32)` with a `bool` element
must raise `TypeError` from the marshaller before any HTTP call.

### What the integration test specifically catches

The current value-typed marshaller fails on `DateTime64(3)`:
`datetime.isoformat(timespec="seconds")` truncates the millisecond
field to zero, so the SELECT for row 1's `dt64_3` returns nothing.
The new type-driven marshaller produces `"2026-05-09 12:34:56.789"`
and the SELECT matches. This is the regression the integration test
guards.

---

## Implementation order

1. Add `_parse_placeholder_types` and the new typed `_marshal_param` in
   `queries.py`, along with the leaf handlers and the Array/Nullable
   wrappers. Wire `query_as_user` to use both.
2. Rewrite the existing unit tests to pass `ch_type`. Add the new unit
   tests covering placeholder parsing, leaf types, recursion, and
   strictness. Verify they pass without the testcontainer.
3. Add the integration test file. Verify against the testcontainer
   that every typed roundtrip works and `dt64_3` matches to the
   millisecond.
4. Final lint + typecheck + full pytest sweep.

Each step is a separate commit.

## Risks

- **`bool` vs `int` precedence in dispatch.** The leaf handlers for
  `Int*`/`UInt*`/`Float*` must check `isinstance(v, bool)` first and
  reject. Without it, `True` slips through `isinstance(int)` and
  produces the literal string `"True"`, which CH then rejects with
  an opaque error.
- **Nested-type regex.** The placeholder regex assumes types don't
  contain `}`. CH's actual grammar permits no `}` in any current
  type; if a future type ever does, the regex needs upgrading.
- **Test row count vs WHERE matching.** Row 2 (with `n_s = NULL`)
  must not match the populated WHERE clauses. The plan inserts row 2
  with the same values as row 1 except for `n_s`, so per-type WHERE
  clauses on populated columns match BOTH rows. Concretely the
  per-type asserts must filter on `WHERE col = {p:Type} AND id = 1`
  (or insert row 2 with strictly different non-`n_s` values). The
  implementation plan locks in one of these — see the plan.
- **Naive datetime as UTC.** This is a project convention rather
  than a hard rule. Documented in the marshaller's docstring so a
  surprised future reader sees the why.

## Out of scope (deferred)

- Decimal, UUID, Map, Tuple, LowCardinality, Enum (per type-coverage
  decision).
- Caller-side type wrappers.
- Round-tripping via `query_as_user`'s `EXECUTE AS` path. The
  integration test exercises the marshaller directly via the same
  CH HTTP endpoint, so impersonation setup isn't needed.
