# Typed CH parameter marshaller — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `_marshal_param` in `src/iris/clickhouse/queries.py` dispatch on the CH type declared in the `{name:Type}` placeholder rather than the Python value's type, with full support for scalar types, the `DateTime`/`DateTime64` family, `Array(T)`, and `Nullable(T)`.

**Architecture:** Parse the SQL once per call to extract a `name → type_str` map. For each parameter, look up its declared type and dispatch to a leaf handler that knows how to format that type. Wrap types `Array(T)` and `Nullable(T)` recurse; everything else hits a flat dispatch.

**Tech Stack:** Python 3.13, httpx (async), clickhouse-connect (only for the testcontainer admin client), pytest, testcontainers.

**Spec:** `docs/superpowers/specs/2026-05-09-typed-param-marshaller-design.md`.

**Conventions you must respect:**
- DDL safety: external strings flow through `validate_identifier` + `quote_identifier`. Never f-string-concat raw user input into SQL.
- Tests live under `tests/` (sibling to `src/`), no `__init__.py` under `tests/`, every test file basename is unique.
- Lint: `uv run ruff check` must produce zero warnings.
- Type: `uv run basedpyright --level error` and `--level warning` must both stay at zero.
- Tests: `uv run pytest` must be green.

---

## File map

| File | Change |
|---|---|
| `src/iris/clickhouse/queries.py` | Add `_parse_placeholder_types(sql)`. Replace `_marshal_param(v)` with `_marshal_param(v, ch_type)` plus leaf handlers. Wire `query_as_user` through both. |
| `tests/clickhouse/test_query_marshaling.py` | Rewrite the existing 8 unit tests for the new signature. Add: parser tests, leaf-type tests for each supported CH type, Array/Nullable recursion, datetime variants, strictness assertion in `query_as_user`. |
| `tests/clickhouse/test_query_marshaling_integration.py` *(new)* | CH-testcontainer integration test that creates a typed table, inserts known rows, runs typed parameterized SELECTs over the HTTP endpoint, and asserts CH returns the expected rows. |

---

## Task 1 — Add `_parse_placeholder_types`

Add the SQL-placeholder regex parser. Pure-Python; no I/O.

**Files:**
- Modify: `src/iris/clickhouse/queries.py`
- Modify: `tests/clickhouse/test_query_marshaling.py`

- [ ] **Step 1: Write failing tests for the parser**

Replace the entire body of `tests/clickhouse/test_query_marshaling.py` with the new structure (we keep the file because of the `--import-mode=importlib` unique-basename rule). Drop the old value-typed tests; we'll re-add type-aware leaf tests in Task 2.

```python
"""Unit tests for the CH HTTP-param marshaller and SQL placeholder parser."""
from __future__ import annotations

import pytest


def _import_parse():
    from iris.clickhouse.queries import _parse_placeholder_types

    return _parse_placeholder_types


def test_parse_basic_placeholder():
    p = _import_parse()
    assert p("SELECT * FROM t WHERE x = {x:Int32}") == {"x": "Int32"}


def test_parse_multiple_placeholders():
    p = _import_parse()
    assert p("WHERE x = {x:Int32} AND s = {s:String}") == {
        "x": "Int32",
        "s": "String",
    }


def test_parse_nested_type_array_of_nullable():
    p = _import_parse()
    assert p("WHERE xs IN ({xs:Array(Nullable(Int32))})") == {
        "xs": "Array(Nullable(Int32))"
    }


def test_parse_nested_type_array_of_string():
    p = _import_parse()
    assert p("WHERE names IN ({names:Array(String)})") == {
        "names": "Array(String)"
    }


def test_parse_datetime64_with_precision():
    p = _import_parse()
    assert p("WHERE ts = {ts:DateTime64(3)}") == {"ts": "DateTime64(3)"}


def test_parse_datetime_with_timezone_arg():
    p = _import_parse()
    assert p("WHERE ts = {ts:DateTime('UTC')}") == {"ts": "DateTime('UTC')"}


def test_parse_repeated_name_same_type_is_one_entry():
    p = _import_parse()
    sql = "WHERE a = {u:String} OR b = {u:String}"
    assert p(sql) == {"u": "String"}


def test_parse_repeated_name_conflicting_types_raises():
    p = _import_parse()
    sql = "WHERE a = {u:String} OR b = {u:Int32}"
    with pytest.raises(ValueError, match="conflicting CH types for placeholder 'u'"):
        p(sql)


def test_parse_no_placeholders_is_empty():
    p = _import_parse()
    assert p("SELECT 1") == {}


def test_parse_trims_whitespace_inside_type():
    p = _import_parse()
    # CH accepts whitespace inside parametric types like Decimal(10, 2);
    # we trim leading/trailing only, preserve interior.
    assert p("WHERE x = {x: Int32 }") == {"x": "Int32"}
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/clickhouse/test_query_marshaling.py -v`
Expected: every test fails with `ImportError: cannot import name '_parse_placeholder_types'`.

- [ ] **Step 3: Implement `_parse_placeholder_types`**

In `src/iris/clickhouse/queries.py`, add `import re` to the imports (preserve `from __future__ import annotations` at top), and add this function above `_marshal_param`:

```python
_PLACEHOLDER_RE = re.compile(r"\{(\w+):([^}]+)\}")


def _parse_placeholder_types(sql: str) -> dict[str, str]:
    """Extract every ``{name:Type}`` placeholder from ``sql``.

    Returns a ``name -> type-string`` map. Type strings are stripped of
    leading/trailing whitespace; interior whitespace (e.g. inside
    ``Decimal(10, 2)``) is preserved.

    Raises ``ValueError`` if the same name appears with conflicting types
    in two different placeholders. The same name with the same type is
    allowed (collapses to one entry).

    The regex relies on CH type strings never containing ``}``; nested
    parametric types like ``Array(Nullable(Int32))`` stay inside parens.
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

Required new top-of-file import (alongside the existing imports):

```python
import re
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/clickhouse/test_query_marshaling.py -v`
Expected: all 10 tests pass.

- [ ] **Step 5: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/iris/clickhouse/queries.py tests/clickhouse/test_query_marshaling.py
git commit -m "feat(queries): add _parse_placeholder_types for CH SQL"
```

---

## Task 2 — Replace `_marshal_param` with the type-driven version

Replace the value-typed marshaller with one that takes the declared CH type and dispatches on it. Add leaf handlers. Add unit tests for every supported type.

**Files:**
- Modify: `src/iris/clickhouse/queries.py`
- Modify: `tests/clickhouse/test_query_marshaling.py`

- [ ] **Step 1: Write failing tests for every leaf type**

Append to `tests/clickhouse/test_query_marshaling.py`:

```python
from datetime import UTC, date, datetime, timezone


def _import_marshal():
    from iris.clickhouse.queries import _marshal_param

    return _marshal_param


# ---- String ---------------------------------------------------------------


def test_marshal_string_passes_through():
    m = _import_marshal()
    assert m("hello", "String") == "hello"


def test_marshal_string_preserves_unicode_and_quotes():
    m = _import_marshal()
    assert m("O'Brien — élan", "String") == "O'Brien — élan"


def test_marshal_fixed_string_passes_through():
    m = _import_marshal()
    assert m("abcdefgh", "FixedString(8)") == "abcdefgh"


# ---- Integers -------------------------------------------------------------


@pytest.mark.parametrize("ch_type", ["Int8", "Int16", "Int32", "Int64"])
def test_marshal_signed_int(ch_type):
    m = _import_marshal()
    assert m(-7, ch_type) == "-7"
    assert m(0, ch_type) == "0"


@pytest.mark.parametrize("ch_type", ["UInt8", "UInt16", "UInt32", "UInt64"])
def test_marshal_unsigned_int(ch_type):
    m = _import_marshal()
    assert m(42, ch_type) == "42"


def test_marshal_int_rejects_bool():
    """bool is a Python int subclass; the int handlers must reject it
    before the isinstance(int) branch swallows True/False as 1/0 strings."""
    m = _import_marshal()
    with pytest.raises(TypeError, match="bool"):
        m(True, "Int32")
    with pytest.raises(TypeError, match="bool"):
        m(False, "UInt8")


# ---- Floats ---------------------------------------------------------------


@pytest.mark.parametrize("ch_type", ["Float32", "Float64"])
def test_marshal_float(ch_type):
    m = _import_marshal()
    assert m(3.14, ch_type) == "3.14"


def test_marshal_float_accepts_int():
    m = _import_marshal()
    assert m(42, "Float64") == "42"


def test_marshal_float_rejects_bool():
    m = _import_marshal()
    with pytest.raises(TypeError, match="bool"):
        m(True, "Float64")


# ---- Bool -----------------------------------------------------------------


def test_marshal_bool_true():
    m = _import_marshal()
    assert m(True, "Bool") == "true"


def test_marshal_bool_false():
    m = _import_marshal()
    assert m(False, "Bool") == "false"


# ---- Date / Date32 --------------------------------------------------------


@pytest.mark.parametrize("ch_type", ["Date", "Date32"])
def test_marshal_date_from_date_value(ch_type):
    m = _import_marshal()
    assert m(date(2026, 5, 9), ch_type) == "2026-05-09"


@pytest.mark.parametrize("ch_type", ["Date", "Date32"])
def test_marshal_date_from_datetime_value(ch_type):
    m = _import_marshal()
    assert m(datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC), ch_type) == "2026-05-09"


# ---- DateTime / DateTime('TZ') --------------------------------------------


def test_marshal_datetime_naive_treated_as_utc():
    m = _import_marshal()
    assert m(datetime(2026, 5, 9, 12, 0, 0), "DateTime") == "2026-05-09 12:00:00"


def test_marshal_datetime_aware_utc():
    m = _import_marshal()
    assert m(datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC), "DateTime") == (
        "2026-05-09 12:00:00"
    )


def test_marshal_datetime_aware_non_utc_converts_to_utc():
    m = _import_marshal()
    plus2 = timezone(__import__("datetime").timedelta(hours=2))
    # 14:00 +02:00 == 12:00 UTC.
    assert m(datetime(2026, 5, 9, 14, 0, 0, tzinfo=plus2), "DateTime") == (
        "2026-05-09 12:00:00"
    )


def test_marshal_datetime_with_timezone_arg_uses_same_handler():
    m = _import_marshal()
    assert m(datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC), "DateTime('UTC')") == (
        "2026-05-09 12:00:00"
    )


def test_marshal_datetime_truncates_subsecond_for_plain_datetime():
    """Plain DateTime has second precision; sub-second is dropped."""
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 0, 0, 789000, tzinfo=UTC)
    assert m(val, "DateTime") == "2026-05-09 12:00:00"


# ---- DateTime64(p) --------------------------------------------------------


def test_marshal_datetime64_3_preserves_milliseconds():
    """The original bug: DateTime64(3) needs '.789'; the old code dropped it."""
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 789000, tzinfo=UTC)
    assert m(val, "DateTime64(3)") == "2026-05-09 12:34:56.789"


def test_marshal_datetime64_6_preserves_microseconds():
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 123456, tzinfo=UTC)
    assert m(val, "DateTime64(6)") == "2026-05-09 12:34:56.123456"


def test_marshal_datetime64_0_drops_fractional():
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 999999, tzinfo=UTC)
    assert m(val, "DateTime64(0)") == "2026-05-09 12:34:56"


def test_marshal_datetime64_3_truncates_microseconds():
    """Microsecond precision higher than the declared (3) gets truncated."""
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 789999, tzinfo=UTC)
    # 789999 us -> 789 ms (truncates)
    assert m(val, "DateTime64(3)") == "2026-05-09 12:34:56.789"


def test_marshal_datetime64_9_pads_with_zeros():
    """DateTime64(9) wants 9 digits; Python only has 6 us, so pad with zeros."""
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 123456, tzinfo=UTC)
    assert m(val, "DateTime64(9)") == "2026-05-09 12:34:56.123456000"


# ---- Array(T) -------------------------------------------------------------


def test_marshal_array_of_int():
    m = _import_marshal()
    assert m([1, 2, 3], "Array(Int32)") == "[1,2,3]"


def test_marshal_array_of_string_quotes_each_element():
    m = _import_marshal()
    assert m(["alice", "bob"], "Array(String)") == "['alice','bob']"


def test_marshal_array_of_string_escapes_quote_and_backslash():
    m = _import_marshal()
    assert m(["O'Brien"], "Array(String)") == "['O\\'Brien']"
    assert m(["with\\backslash"], "Array(String)") == "['with\\\\backslash']"


def test_marshal_array_accepts_tuple():
    m = _import_marshal()
    assert m((1, 2), "Array(Int32)") == "[1,2]"


def test_marshal_array_empty():
    m = _import_marshal()
    assert m([], "Array(Int32)") == "[]"


# ---- Nullable(T) ----------------------------------------------------------


def test_marshal_nullable_none_is_NULL():
    m = _import_marshal()
    assert m(None, "Nullable(String)") == "NULL"
    assert m(None, "Nullable(Int32)") == "NULL"


def test_marshal_nullable_value_falls_through_to_inner():
    m = _import_marshal()
    assert m("hello", "Nullable(String)") == "hello"
    assert m(42, "Nullable(Int32)") == "42"


# ---- Combined wrappers ----------------------------------------------------


def test_marshal_array_of_nullable_int():
    m = _import_marshal()
    assert m([1, None, 3], "Array(Nullable(Int32))") == "[1,NULL,3]"


def test_marshal_nullable_array():
    """Nullable(Array(T)) unwraps the Nullable first."""
    m = _import_marshal()
    assert m(None, "Nullable(Array(Int32))") == "NULL"
    assert m([1, 2], "Nullable(Array(Int32))") == "[1,2]"


# ---- Unsupported types ----------------------------------------------------


def test_marshal_unknown_type_raises():
    m = _import_marshal()
    with pytest.raises(TypeError, match="unsupported CH param type"):
        m(b"\\x00", "Decimal(10, 2)")


def test_marshal_unsupported_python_value_raises():
    """Even with String, a non-string value is rejected."""
    m = _import_marshal()
    with pytest.raises(TypeError):
        m(42, "String")
```

- [ ] **Step 2: Run tests to verify they all fail**

Run: `uv run pytest tests/clickhouse/test_query_marshaling.py -v`
Expected: many failures — `_marshal_param` exists but with the old single-arg signature, so type-arg calls error out, and the type-aware behavior isn't implemented.

- [ ] **Step 3: Replace `_marshal_param` and add the leaf handlers**

In `src/iris/clickhouse/queries.py`:

1. Update the datetime import to bring in `date` and `UTC`. Replace:

   ```python
   from datetime import datetime
   ```

   with:

   ```python
   from datetime import UTC, date, datetime
   ```

2. Below the existing `_parse_placeholder_types` (added in Task 1), replace the existing `_marshal_param` function with the typed implementation. The full block to add (after `_parse_placeholder_types`):

```python
_DATETIME64_RE = re.compile(r"^DateTime64\((\d+)\)$")
_DATETIME_TZ_RE = re.compile(r"^DateTime(?:\([^)]*\))?$")
_FIXED_STRING_RE = re.compile(r"^FixedString\(\d+\)$")
_INT_TYPES = frozenset(
    {"Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64"}
)
_FLOAT_TYPES = frozenset({"Float32", "Float64"})


def _format_datetime_seconds(v: datetime) -> str:
    """Format ``v`` as ``YYYY-MM-DD HH:MM:SS`` in UTC.

    Naive datetimes are treated as UTC (matches iris's repo-wide
    convention of ``datetime.now(UTC)``); aware datetimes are converted.
    """
    if v.tzinfo is None:
        v = v.replace(tzinfo=UTC)
    else:
        v = v.astimezone(UTC)
    return v.strftime("%Y-%m-%d %H:%M:%S")


def _format_datetime64(v: datetime, precision: int) -> str:
    """Format ``v`` as ``YYYY-MM-DD HH:MM:SS.fff…`` with ``precision``
    fractional digits, in UTC. Python ``datetime`` carries microsecond
    precision (6 digits); higher precisions right-pad with zeros, lower
    precisions truncate."""
    seconds = _format_datetime_seconds(v)
    if precision == 0:
        return seconds
    micros = (v.microsecond if v.tzinfo is None else v.astimezone(UTC).microsecond)
    # 6-digit microsecond field; pad on the right to up to 9 digits, then
    # take the first ``precision`` digits.
    fractional = f"{micros:06d}".ljust(9, "0")[:precision]
    return f"{seconds}.{fractional}"


def _format_array(v: object, inner_type: str) -> str:
    if not isinstance(v, (list, tuple)):
        raise TypeError(
            f"Array({inner_type}) expects list or tuple, got {type(v).__name__}"
        )
    parts = [_marshal_array_element(e, inner_type) for e in v]
    return "[" + ",".join(parts) + "]"


def _marshal_array_element(v: object, ch_type: str) -> str:
    """Like ``_marshal_param`` but quotes Strings (CH's array literal
    syntax requires single-quoted string elements). Nullable inside an
    array still emits the bare ``NULL`` token."""
    ch_type = ch_type.strip()
    if ch_type.startswith("Nullable(") and ch_type.endswith(")"):
        if v is None:
            return "NULL"
        return _marshal_array_element(v, ch_type[len("Nullable(") : -1])
    if ch_type == "String" or _FIXED_STRING_RE.match(ch_type):
        if not isinstance(v, str):
            raise TypeError(
                f"{ch_type} expects str, got {type(v).__name__}"
            )
        # Backslash first, then single quote — order matters.
        escaped = v.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    return _marshal_param(v, ch_type)


def _marshal_param(v: object, ch_type: str) -> str:
    """Marshal a Python value to the wire format CH expects for the
    declared placeholder type ``ch_type``.

    The full grammar is in
    ``docs/superpowers/specs/2026-05-09-typed-param-marshaller-design.md``.

    Briefly: dispatches on the declared CH type, peeling ``Nullable(...)``
    and ``Array(...)`` wrappers recursively. Raises ``TypeError`` for
    unsupported types and value/type mismatches.
    """
    ch_type = ch_type.strip()

    # Nullable(T): None → "NULL", else recurse on inner T.
    if ch_type.startswith("Nullable(") and ch_type.endswith(")"):
        inner = ch_type[len("Nullable(") : -1]
        if v is None:
            return "NULL"
        return _marshal_param(v, inner)

    # Array(T): bracket-comma-join, with strings quoted inside.
    if ch_type.startswith("Array(") and ch_type.endswith(")"):
        inner = ch_type[len("Array(") : -1]
        return _format_array(v, inner)

    # bool — checked before any int-like branch because bool subclasses int.
    if ch_type == "Bool":
        if not isinstance(v, bool):
            raise TypeError(f"Bool expects bool, got {type(v).__name__}")
        return "true" if v else "false"

    # Numbers — must reject bool first.
    if ch_type in _INT_TYPES:
        if isinstance(v, bool):
            raise TypeError(f"{ch_type} rejects bool (use Bool type)")
        if not isinstance(v, int):
            raise TypeError(f"{ch_type} expects int, got {type(v).__name__}")
        return str(v)
    if ch_type in _FLOAT_TYPES:
        if isinstance(v, bool):
            raise TypeError(f"{ch_type} rejects bool")
        if not isinstance(v, (int, float)):
            raise TypeError(
                f"{ch_type} expects int or float, got {type(v).__name__}"
            )
        return str(v)

    # String / FixedString(N): bare passthrough; quoting only happens inside arrays.
    if ch_type == "String" or _FIXED_STRING_RE.match(ch_type):
        if not isinstance(v, str):
            raise TypeError(f"{ch_type} expects str, got {type(v).__name__}")
        return v

    # Date / Date32 — accepts both date and datetime, formats YYYY-MM-DD.
    if ch_type in ("Date", "Date32"):
        if isinstance(v, datetime):
            return v.date().isoformat()
        if isinstance(v, date):
            return v.isoformat()
        raise TypeError(f"{ch_type} expects date or datetime, got {type(v).__name__}")

    # DateTime64(p) — must come BEFORE DateTime since the latter regex
    # matches "DateTime" with optional "(...)".
    m64 = _DATETIME64_RE.match(ch_type)
    if m64:
        if not isinstance(v, datetime):
            raise TypeError(f"{ch_type} expects datetime, got {type(v).__name__}")
        return _format_datetime64(v, int(m64.group(1)))

    # DateTime / DateTime('TZ') — second precision in UTC.
    if _DATETIME_TZ_RE.match(ch_type):
        if not isinstance(v, datetime):
            raise TypeError(f"{ch_type} expects datetime, got {type(v).__name__}")
        return _format_datetime_seconds(v)

    raise TypeError(f"unsupported CH param type: {ch_type!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/clickhouse/test_query_marshaling.py -v`
Expected: all tests pass.

- [ ] **Step 5: Run the full pytest suite to verify no regression**

Run: `uv run pytest -x`
Expected: all 302 (or 302 + new) tests pass.

- [ ] **Step 6: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/iris/clickhouse/queries.py tests/clickhouse/test_query_marshaling.py
git commit -m "fix(queries): type-driven CH param marshaller with Array/Nullable/DateTime64 support"
```

---

## Task 3 — Wire `query_as_user` to use the typed marshaller

`query_as_user` now needs to parse SQL placeholders, look up each parameter's declared type, and route through the new `_marshal_param(v, ch_type)`. Add a strictness check: a parameter passed without a matching SQL placeholder is a caller bug.

**Files:**
- Modify: `src/iris/clickhouse/queries.py`
- Modify: `tests/clickhouse/test_query_marshaling.py`

- [ ] **Step 1: Write a failing strictness test**

Append to `tests/clickhouse/test_query_marshaling.py`:

```python
import asyncio


def test_query_as_user_rejects_unbound_parameter():
    """A parameter key that has no matching {name:Type} in the SQL is a
    caller bug — likely a typo. The error names the offending key."""
    import httpx

    from iris.clickhouse.queries import query_as_user

    # An async transport that records nothing — we expect the call to fail
    # before any HTTP request is sent.
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(500, content=b"should not be reached")
    )
    http_client = httpx.AsyncClient(
        base_url="http://stub", transport=transport
    )

    async def _run():
        try:
            await query_as_user(
                http_client,
                username="alice",
                sql="SELECT * FROM t WHERE u = {u:String}",
                parameters={"u": "alice", "typo": 1},
            )
        finally:
            await http_client.aclose()

    with pytest.raises(ValueError, match="'typo'"):
        asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/clickhouse/test_query_marshaling.py::test_query_as_user_rejects_unbound_parameter -v`
Expected: FAIL — current code silently sends the unbound `param_typo` query string.

- [ ] **Step 3: Update `query_as_user` to use the parser + typed marshaller**

In `src/iris/clickhouse/queries.py`, replace the body of `query_as_user` (everything from `body = f"EXECUTE AS …"` through `return [json.loads(...) ...]`) with:

```python
async def query_as_user(
    http_client: httpx.AsyncClient,
    *,
    username: str,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
    database: str | None = None,
) -> list[dict[str, Any]]:
    """Run ``sql`` on ClickHouse impersonated as ``username``.

    Sends ``EXECUTE AS <username> <sql>`` to the CH HTTP endpoint with
    ``default_format=JSONEachRow`` (and ``database=<database>`` when
    supplied, so unqualified table names resolve against that schema).

    Each parameter is marshaled according to the CH type declared in the
    SQL via ``{name:Type}``. A parameter passed without a matching
    placeholder raises ``ValueError`` — likely a caller-side typo.
    """
    body = f"EXECUTE AS {quote_identifier(username, kind='username')} {sql}"
    params: dict[str, str] = {"default_format": "JSONEachRow"}
    if database:
        params["database"] = database
    if parameters:
        type_map = _parse_placeholder_types(sql)
        for k, v in parameters.items():
            if k not in type_map:
                raise ValueError(
                    f"parameter {k!r} has no {{{k}:Type}} placeholder in the SQL"
                )
            params[f"param_{k}"] = _marshal_param(v, type_map[k])
    response = await http_client.post("/", params=params, content=body)
    response.raise_for_status()
    text = response.text.strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line]
```

- [ ] **Step 4: Run the new test**

Run: `uv run pytest tests/clickhouse/test_query_marshaling.py::test_query_as_user_rejects_unbound_parameter -v`
Expected: PASS.

- [ ] **Step 5: Run the full pytest suite**

Run: `uv run pytest -x`
Expected: all tests pass. The existing `query_as_user` callers all use `{u:String}` / `{r:String}` patterns that already declare a type; nothing breaks.

- [ ] **Step 6: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/iris/clickhouse/queries.py tests/clickhouse/test_query_marshaling.py
git commit -m "fix(queries): query_as_user dispatches each param on its declared CH type"
```

---

## Task 4 — Integration test: CH-roundtrip across all supported types

Verify that the marshaller's wire format is what CH actually parses. Builds a real table, inserts known values, queries each typed column.

**Files:**
- Create: `tests/clickhouse/test_query_marshaling_integration.py`

- [ ] **Step 1: Write the integration test**

Create `tests/clickhouse/test_query_marshaling_integration.py`:

```python
"""CH-roundtrip integration test for the typed parameter marshaller.

Builds a table covering every supported CH type, inserts known values,
runs typed parameterized SELECTs through CH's HTTP endpoint, and asserts
each query returns the expected row.

Bypasses ``EXECUTE AS`` — the test exercises the marshaller pipeline
directly via the same HTTP endpoint ``query_as_user`` uses, without
needing a separate test user with IMPERSONATE granted.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime

import httpx
import pytest

from iris.clickhouse.queries import _marshal_param, _parse_placeholder_types


async def _query_typed(
    http: httpx.AsyncClient,
    *,
    sql: str,
    parameters: dict[str, object],
) -> list[dict[str, object]]:
    """Mirror of query_as_user without the EXECUTE AS prefix.

    Same parser + marshaller pipeline; pure HTTP call against CH.
    """
    type_map = _parse_placeholder_types(sql)
    params: dict[str, str] = {"default_format": "JSONEachRow"}
    for k, v in parameters.items():
        params[f"param_{k}"] = _marshal_param(v, type_map[k])
    response = await http.post("/", params=params, content=sql)
    response.raise_for_status()
    text = response.text.strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line]


@pytest.fixture
def http_client(ch_settings):
    """An httpx.AsyncClient pointed at the CH testcontainer.

    Uses iris_svc credentials from ch_settings — the same identity that
    holds CREATE/INSERT/SELECT on the test table.
    """
    base_url = f"http://{ch_settings.host}:{ch_settings.port}"
    client = httpx.AsyncClient(
        base_url=base_url,
        auth=(ch_settings.user, ch_settings.password),
        timeout=httpx.Timeout(30.0),
    )
    try:
        yield client
    finally:
        asyncio.run(client.aclose())


@pytest.fixture
def marshal_table(ch_client, prefix):
    """Per-test table with one column per supported CH type, plus two
    rows: row 1 fully populated, row 2 with n_s = NULL (everything else
    matches row 1 so per-type WHERE clauses can disambiguate via id)."""
    table = f"{prefix}_marshal_check"
    ch_client.command(
        f"""
        CREATE TABLE `{table}` (
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
        """
    )
    ch_client.command(
        f"""
        INSERT INTO `{table}` VALUES
            (1, 'hello', 'abcdefgh', 200, -7, 18446744073709551615, 3.14, true,
             '2026-05-09', '2026-05-09 12:00:00', '2026-05-09 12:00:00',
             '2026-05-09 12:34:56.789',
             ['alice','bob'], [1,2,3], 'filled', [1, NULL, 3]),
            (2, 'hello', 'abcdefgh', 200, -7, 18446744073709551615, 3.14, true,
             '2026-05-09', '2026-05-09 12:00:00', '2026-05-09 12:00:00',
             '2026-05-09 12:34:56.789',
             ['alice','bob'], [1,2,3], NULL, [1, NULL, 3])
        """
    )
    return table


# Each entry: (column, ch_type, python_value).
# WHERE col = {p:Type} AND id = {pid:Int32} should match exactly id=1.
_ROUNDTRIP_CASES = [
    ("s", "String", "hello"),
    ("fs", "FixedString(8)", "abcdefgh"),
    ("u8", "UInt8", 200),
    ("i32", "Int32", -7),
    ("u64", "UInt64", 18446744073709551615),
    ("f64", "Float64", 3.14),
    ("b", "Bool", True),
    ("d", "Date", date(2026, 5, 9)),
    ("dt", "DateTime", datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)),
    ("dt_tz", "DateTime('UTC')", datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)),
    (
        "dt64_3",
        "DateTime64(3)",
        datetime(2026, 5, 9, 12, 34, 56, 789000, tzinfo=UTC),
    ),
    ("arr_s", "Array(String)", ["alice", "bob"]),
    ("arr_i", "Array(Int32)", [1, 2, 3]),
    ("n_s", "Nullable(String)", "filled"),
    ("arr_n_i", "Array(Nullable(Int32))", [1, None, 3]),
]


@pytest.mark.parametrize("col,ch_type,value", _ROUNDTRIP_CASES)
def test_marshal_roundtrip_returns_expected_row(
    http_client, marshal_table, col, ch_type, value
):
    """For each typed column: WHERE col = {p:Type} AND id = 1 returns row 1.

    Critically, dt64_3's value carries .789 milliseconds — the marshaller
    must preserve them or the WHERE clause won't match (this is the
    regression the type-driven dispatch fixes).
    """
    sql = (
        f"SELECT id FROM `{marshal_table}` "
        f"WHERE `{col}` = {{p:{ch_type}}} AND id = {{pid:Int32}}"
    )
    rows = asyncio.run(
        _query_typed(http_client, sql=sql, parameters={"p": value, "pid": 1})
    )
    assert rows == [{"id": 1}], (
        f"{col} ({ch_type}) roundtrip failed; rows={rows}"
    )


def test_nullable_string_null_row_matches_via_is_null(
    http_client, marshal_table
):
    """Row 2 has n_s = NULL. ``WHERE n_s = NULL`` is always false in SQL;
    use IS NULL. This validates that NULL values are stored correctly
    (and is the natural pair test for the populated-Nullable case)."""
    sql = f"SELECT id FROM `{marshal_table}` WHERE n_s IS NULL ORDER BY id"
    rows = asyncio.run(_query_typed(http_client, sql=sql, parameters={}))
    assert rows == [{"id": 2}]


def test_array_of_nullable_with_null_element(
    http_client, marshal_table
):
    """Round-trip a list containing None inside Array(Nullable(Int32))."""
    sql = (
        f"SELECT id FROM `{marshal_table}` "
        f"WHERE arr_n_i = {{p:Array(Nullable(Int32))}} AND id = {{pid:Int32}}"
    )
    rows = asyncio.run(
        _query_typed(
            http_client,
            sql=sql,
            parameters={"p": [1, None, 3], "pid": 1},
        )
    )
    assert rows == [{"id": 1}]


def test_marshal_array_with_bool_element_raises_before_http(
    http_client, marshal_table
):
    """``Array(Int32)`` with a bool element should raise TypeError from
    the marshaller before any HTTP call — bool is rejected by the int
    branch."""
    sql = (
        f"SELECT id FROM `{marshal_table}` "
        f"WHERE arr_i = {{p:Array(Int32)}} AND id = {{pid:Int32}}"
    )

    async def _run():
        await _query_typed(
            http_client,
            sql=sql,
            parameters={"p": [1, True, 3], "pid": 1},
        )

    with pytest.raises(TypeError, match="rejects bool"):
        asyncio.run(_run())
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/clickhouse/test_query_marshaling_integration.py -v`
Expected: all tests pass. The `dt64_3` parametrized case is the key regression test: it would fail under the old marshaller because milliseconds got truncated.

- [ ] **Step 3: Run the full pytest suite**

Run: `uv run pytest -x`
Expected: every test passes.

- [ ] **Step 4: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/clickhouse/test_query_marshaling_integration.py
git commit -m "test(queries): CH-roundtrip integration test for typed param marshaller"
```

---

## Final verification

After all four tasks land:

- [ ] **Run the entire suite once more from clean.**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
uv run pytest
```

Expected: all clean.

- [ ] **Skim `git log --oneline main..HEAD`** — should be 4 commits, each with a descriptive subject.

- [ ] **Sanity check the leaf coverage:** `grep -nE "(Int32|UInt8|DateTime64|Array|Nullable|Bool|String)" src/iris/clickhouse/queries.py` should show every supported type referenced in the dispatch.

---

## Self-review notes

This plan was checked against the spec section-by-section.

| Spec section | Tasks covering it |
|---|---|
| `_parse_placeholder_types` design | Task 1 |
| `_marshal_param(v, ch_type)` dispatch table (all leaf types) | Task 2 |
| Array(T) recursion + string element quoting | Task 2 |
| Nullable(T) recursion | Task 2 |
| `bool` rejection in `Int*`/`UInt*`/`Float*` branches | Task 2 |
| Datetime semantics (naive→UTC, aware→UTC) | Task 2 |
| DateTime64(p) precision-aware fractional formatting | Task 2 (unit) + Task 4 (integration) |
| Strictness: unbound parameter raises | Task 3 |
| `query_as_user` plumbing | Task 3 |
| Roundtrip integration test against CH | Task 4 |

No placeholders. No "TBD" or "similar to". Every code block is complete and self-contained.

Type/method consistency: `_parse_placeholder_types`, `_marshal_param(v, ch_type)`, `_marshal_array_element`, `_format_datetime_seconds`, `_format_datetime64`, `_format_array`, `_DATETIME64_RE`, `_DATETIME_TZ_RE`, `_FIXED_STRING_RE`, `_INT_TYPES`, `_FLOAT_TYPES` are all referenced consistently across tasks.

The integration test resolves the row-disambiguation risk flagged in the spec: every typed-roundtrip query carries `AND id = {pid:Int32}` so it matches exactly row 1, regardless of how many other rows share the same column value.
