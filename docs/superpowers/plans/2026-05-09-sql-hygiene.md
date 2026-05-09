# SQL / identifier hygiene — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (the user's CLAUDE.md mandates Inline Execution over Subagent-Driven). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 4 review-surfaced SQL/identifier hygiene findings as one cohesive pass: reject reserved suffixes in `validate_identifier`; dedup `_FIXED_STRING_RE`; rename `quote_string` → `quote_sql_literal` and add a sibling `quote_sql_array_element`; sweep orphan grants in `delete_database` before the DROP.

**Architecture:** Each fix is small and self-contained. The whole bundle lands in **one atomic commit** at Task 7. Intermediate tasks stage edits without committing — the final task runs ruff + basedpyright (warning level) + the full pytest suite (incl. integration) as the merge gate before the commit.

**Tech Stack:** Python 3.13, FastAPI 0.136, ClickHouse 24+ (testcontainer), basedpyright, ruff, pytest 9.

**Source spec:** `docs/superpowers/specs/2026-05-09-sql-hygiene-design.md`.

**Pre-condition:** the auth-reshape (`b11cf20`) and security-hardening (`75a60ad`) specs landed on `main`. All file/symbol names below assume that layout (`Capabilities`, `iris.auth.store`, `iris.auth.views`, `iris.clickhouse.capabilities`, `iris.auth.client_ip`).

---

## Task 1: Create feature branch and verify baseline

**Files:** none modified.

- [ ] **Step 1.1: Create the feature branch**

```bash
git -C /home/driou/dev/project/iris checkout -b feature/sql-hygiene
```

- [ ] **Step 1.2: Verify a clean baseline — must be green BEFORE we start**

```bash
uv run --project /home/driou/dev/project/iris ruff check
uv run --project /home/driou/dev/project/iris basedpyright --level warning
uv run --project /home/driou/dev/project/iris pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
```

Expected:
- ruff: zero warnings.
- basedpyright: zero errors, zero warnings.
- pytest: **402 passed** (376 baseline + 26 new from security-hardening). If anything fails, stop — don't conflate refactor breakage with pre-existing breakage.

- [ ] **Step 1.3: Capture pre-spec test inventory for the no-regression diff**

```bash
uv run --project /home/driou/dev/project/iris pytest --collect-only -q --ignore=tests/auth/integration --ignore=tests/clickhouse/integration > /tmp/pytest-inventory-before.txt
wc -l /tmp/pytest-inventory-before.txt
```

Expected line count: **404** (one line per test plus the trailer).

- [ ] **Step 1.4: Do NOT commit.** This task is verification only.

---

## Task 2: Suffix-block in `validate_identifier`

**Files:**
- Modify: `src/iris/clickhouse/identifiers.py` — add `_RESERVED_SUFFIXES`, `_SUFFIX_CHECKED_KINDS`, suffix-block branch in `validate_identifier`.
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py` — add 7 new tests.

- [ ] **Step 2.1: Add the failing tests**

Append to `tests/clickhouse/test_clickhouse_identifiers.py` (after the existing tests, before `test_public_surface_exports_named_symbols`). The file already imports `pytest` at the top — no new imports needed:

```python
_RESERVED_SUFFIX_VALUES = ("_USER", "_GRP", "_DBADMIN", "_DBWRITER", "_DBREADER")


@pytest.mark.parametrize("suffix", _RESERVED_SUFFIX_VALUES)
def test_validate_identifier_rejects_reserved_suffix_for_database(suffix):
    with pytest.raises(InvalidIdentifierError, match=suffix):
        validate_identifier(f"foo{suffix}", kind="database")


@pytest.mark.parametrize("suffix", _RESERVED_SUFFIX_VALUES)
def test_validate_identifier_rejects_reserved_suffix_for_username(suffix):
    with pytest.raises(InvalidIdentifierError, match=suffix):
        validate_identifier(f"alice{suffix}", kind="username")


@pytest.mark.parametrize("suffix", _RESERVED_SUFFIX_VALUES)
def test_validate_identifier_rejects_reserved_suffix_for_group(suffix):
    with pytest.raises(InvalidIdentifierError, match=suffix):
        validate_identifier(f"sales{suffix}", kind="group")


def test_validate_identifier_accepts_reserved_suffix_for_role():
    """Tier role names like `<db>_DBADMIN` legitimately end in those
    suffixes; the check must not fire for kind='role'."""
    for suffix in _RESERVED_SUFFIX_VALUES:
        assert validate_identifier(f"foo{suffix}", kind="role") == f"foo{suffix}"


@pytest.mark.parametrize("kind", ["table", "column", "policy"])
def test_validate_identifier_accepts_reserved_suffix_for_other_kinds(kind):
    for suffix in _RESERVED_SUFFIX_VALUES:
        assert validate_identifier(f"foo{suffix}", kind=kind) == f"foo{suffix}"


def test_validate_identifier_accepts_normal_external_names():
    assert validate_identifier("alice", kind="username") == "alice"
    assert validate_identifier("sales", kind="group") == "sales"
    assert validate_identifier("orders", kind="database") == "orders"


def test_validate_identifier_error_message_mentions_offending_suffix():
    """Error text must include the suffix so operators tracing logs see why."""
    try:
        validate_identifier("alice_DBADMIN", kind="username")
    except InvalidIdentifierError as exc:
        msg = str(exc)
        assert "_DBADMIN" in msg, f"suffix not in error message: {msg!r}"
        assert "username" in msg, f"kind not in error message: {msg!r}"
    else:
        pytest.fail("expected InvalidIdentifierError")
```

- [ ] **Step 2.2: Run the new tests — they must fail**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v -k 'reserved_suffix or external_names or offending_suffix' 2>&1 | tail -20
```

Expected: rejection-side tests fail (no suffix-block today). The "accepts" / "external names" / "other kinds" tests pass against the existing implementation.

- [ ] **Step 2.3: Modify `src/iris/clickhouse/identifiers.py`**

Read the current contents first:

```bash
cat src/iris/clickhouse/identifiers.py
```

Then apply two targeted edits.

**Edit A** — replace the imports + module-level constants block. The file currently starts with:

```python
"""Validation and quoting helpers for ClickHouse SQL identifiers and string literals."""

from __future__ import annotations

import hashlib
import re

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")
```

Replace with:

```python
"""Validation and quoting helpers for ClickHouse SQL identifiers and string literals."""

from __future__ import annotations

import hashlib
import re
from typing import Final

_IDENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")

# Suffixes iris synthesizes for role names: `<username>_USER`, `<group>_GRP`,
# `<database>_DBADMIN/_DBWRITER/_DBREADER`. External-input identifiers must
# not end in these — otherwise the post-login role-graph walk in
# `iris.clickhouse.capabilities.derive_capabilities` cannot disambiguate
# whether a role is a tier role or an external name that happens to look
# like one. See `_SUFFIX_CHECKED_KINDS` below for the kinds where this
# rule applies.
_RESERVED_SUFFIXES: Final = ("_USER", "_GRP", "_DBADMIN", "_DBWRITER", "_DBREADER")

# Identifier `kind` values that come from external input (auth provider
# claims, route path / query parameters, operator config). Synthesized
# names like `<db>_DBADMIN` legitimately end in reserved suffixes, so
# `kind in {"role", "policy", "table", "column"}` is exempt.
_SUFFIX_CHECKED_KINDS: Final = frozenset({"database", "username", "group"})
```

**Edit B** — replace the body of `validate_identifier`:

```
old:
def validate_identifier(name: str, *, kind: str) -> str:
    """Reject anything outside ``[a-zA-Z0-9_]+``. Returns ``name`` unchanged on success.

    ``kind`` is woven into the error message ("username", "role", "database", ...) so
    operators tracing a bad input can see where it entered.
    """
    if not _IDENT_RE.fullmatch(name):
        raise InvalidIdentifierError(f"invalid {kind}: {name!r}")
    return name

new:
def validate_identifier(name: str, *, kind: str) -> str:
    """Reject anything outside ``[a-zA-Z0-9_]+``. Returns ``name`` unchanged on success.

    For ``kind`` in ``{"database", "username", "group"}``, additionally rejects
    names ending in iris's reserved role suffixes (``_USER``, ``_GRP``,
    ``_DBADMIN``, ``_DBWRITER``, ``_DBREADER``). These suffixes are reserved
    for synthesized role names (e.g. ``<username>_USER``,
    ``<database>_DBADMIN``); allowing external input to also end with them
    creates ambiguity in the post-login role-graph walk in
    ``iris.clickhouse.capabilities.derive_capabilities``.

    Other ``kind`` values (``role``, ``policy``, ``table``, ``column``) skip
    the suffix check, since synthesized role names like ``<db>_DBADMIN``
    legitimately end in those suffixes.

    ``kind`` is woven into the error message ("username", "role", "database",
    ...) so operators tracing a bad input can see where it entered.
    """
    if not _IDENT_RE.fullmatch(name):
        raise InvalidIdentifierError(f"invalid {kind}: {name!r}")
    if kind in _SUFFIX_CHECKED_KINDS:
        for suffix in _RESERVED_SUFFIXES:
            if name.endswith(suffix):
                raise InvalidIdentifierError(
                    f"invalid {kind}: {name!r} ends with reserved iris role suffix {suffix!r}"
                )
    return name
```

- [ ] **Step 2.4: Run the test file — all pass**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v 2>&1 | tail -20
```

Expected: every test in the file passes (the existing tests plus the 7 new ones).

- [ ] **Step 2.5: Do NOT commit.**

---

## Task 3: Hoist `_FIXED_STRING_RE` to `iris.clickhouse.identifiers`

**Files:**
- Modify: `src/iris/clickhouse/identifiers.py` — add the regex.
- Modify: `src/iris/clickhouse/policies.py` — drop local definition; import from identifiers.
- Modify: `src/iris/clickhouse/queries.py` — drop local definition; import from identifiers.
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py` — add one sanity test.

- [ ] **Step 3.1: Add the failing sanity test**

Append to `tests/clickhouse/test_clickhouse_identifiers.py` (after the suffix-block tests):

```python
def test_fixed_string_re_matches_expected_forms():
    """The hoisted _FIXED_STRING_RE matches `FixedString(N)` and rejects
    plain `String`, `Nullable(...)`, etc."""
    from iris.clickhouse.identifiers import _FIXED_STRING_RE

    assert _FIXED_STRING_RE.match("FixedString(16)") is not None
    assert _FIXED_STRING_RE.match("FixedString(1)") is not None
    assert _FIXED_STRING_RE.match("String") is None
    assert _FIXED_STRING_RE.match("Nullable(String)") is None
    assert _FIXED_STRING_RE.match("FixedString(N)") is None  # not a digit
    assert _FIXED_STRING_RE.match("FixedString()") is None  # missing arg
```

- [ ] **Step 3.2: Run the new test — it must fail (no symbol yet)**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py::test_fixed_string_re_matches_expected_forms -v
```

Expected: `ImportError: cannot import name '_FIXED_STRING_RE' from 'iris.clickhouse.identifiers'`.

- [ ] **Step 3.3: Add `_FIXED_STRING_RE` to `iris.clickhouse.identifiers`**

In `src/iris/clickhouse/identifiers.py`, after the `_SLUG_RE = ...` line, add:

```python
# CH's FixedString(N) type marker. Used by row-policy filter construction
# and the typed param marshaller to detect FixedString variants of
# (Array of) string-like types. Hoisted from policies.py + queries.py so
# both consumers share one source of truth.
_FIXED_STRING_RE: Final = re.compile(r"^FixedString\(\d+\)$")
```

(Place it next to `_RESERVED_SUFFIXES` and `_SUFFIX_CHECKED_KINDS` from Task 2 — order: `_IDENT_RE`, `_SLUG_RE`, `_FIXED_STRING_RE`, then the reserved-suffix constants.)

- [ ] **Step 3.4: Drop the local copy in `policies.py` and import from identifiers**

In `src/iris/clickhouse/policies.py`, replace:

```
old:
import re
from typing import cast

from clickhouse_connect.driver.client import Client

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import (
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)


_FIXED_STRING_RE = re.compile(r"^FixedString\(\d+\)$")

new:
from typing import cast

from clickhouse_connect.driver.client import Client

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import (
    _FIXED_STRING_RE,
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)
```

(Drops the `import re` because it was only used by the regex; check the rest of `policies.py` doesn't use `re` for anything else — the grep below verifies.)

```bash
grep -n "^import re\|re\." src/iris/clickhouse/policies.py
```

Expected: only the import line is present. After the edit, the `import re` line is gone and there are no remaining `re.` references in the file.

- [ ] **Step 3.5: Drop the local copy in `queries.py` and import from identifiers**

In `src/iris/clickhouse/queries.py`, the existing top-level structure has both `import re` (used by other regexes) and `_FIXED_STRING_RE = re.compile(...)`. Only drop the FixedString line; keep `import re` (other regexes like `_DATETIME64_RE` still need it).

Replace:

```
old:
_DATETIME64_RE = re.compile(r"^DateTime64\((\d+)\)$")
_DATETIME_TZ_RE = re.compile(r"^DateTime(?:\([^)]*\))?$")
_FIXED_STRING_RE = re.compile(r"^FixedString\(\d+\)$")
_INT_TYPES = frozenset(

new:
_DATETIME64_RE = re.compile(r"^DateTime64\((\d+)\)$")
_DATETIME_TZ_RE = re.compile(r"^DateTime(?:\([^)]*\))?$")
_INT_TYPES = frozenset(
```

Add the import. The current import block in `queries.py` is:

```python
from iris.clickhouse.identifiers import quote_identifier
```

Replace with:

```python
from iris.clickhouse.identifiers import _FIXED_STRING_RE, quote_identifier
```

- [ ] **Step 3.6: Run the new test plus the existing tests touching FixedString**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py::test_fixed_string_re_matches_expected_forms tests/clickhouse/test_query_marshaling.py tests/clickhouse/test_clickhouse_policies.py -q
```

Expected: all green.

- [ ] **Step 3.7: Do NOT commit.**

---

## Task 4: Rename `quote_string` → `quote_sql_literal` + add `quote_sql_array_element`

**Files:**
- Modify: `src/iris/clickhouse/identifiers.py` — rename `quote_string` to `quote_sql_literal`; add `quote_sql_array_element`.
- Modify: `src/iris/clickhouse/policies.py` — update the import + 2 call sites + 1 docstring.
- Modify: `src/iris/clickhouse/queries.py` — `_marshal_array_element` delegates to `quote_sql_array_element`; add the import.
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py` — rename existing `quote_string` tests to `quote_sql_literal` (mechanical); add 5 new `quote_sql_array_element` tests.
- Modify: `tests/clickhouse/test_clickhouse_policies.py` — update the docstring reference (line 269).

- [ ] **Step 4.1: Update tests — rename existing `quote_string` tests + add new ones for both helpers**

In `tests/clickhouse/test_clickhouse_identifiers.py`:

**Edit A** — update the import block:

```
old:
from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)

new:
from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    policy_name,
    quote_identifier,
    quote_sql_array_element,
    quote_sql_literal,
    validate_identifier,
)
```

**Edit B** — replace each existing `quote_string` test with the renamed `quote_sql_literal` version:

```
old:
def test_quote_string_wraps_plain_value():
    assert quote_string("EU") == "'EU'"


def test_quote_string_doubles_embedded_single_quotes():
    assert quote_string("O'Brien") == "'O''Brien'"


def test_quote_string_escapes_backslashes():
    assert quote_string(r"a\b") == r"'a\\b'"


def test_quote_string_handles_combined_escapes():
    # backslash must be escaped before quotes, otherwise '\\\'' would be ambiguous
    assert quote_string("a\\'b") == "'a\\\\''b'"

new:
def test_quote_sql_literal_wraps_plain_value():
    assert quote_sql_literal("EU") == "'EU'"


def test_quote_sql_literal_doubles_embedded_single_quotes():
    assert quote_sql_literal("O'Brien") == "'O''Brien'"


def test_quote_sql_literal_escapes_backslashes():
    assert quote_sql_literal(r"a\b") == r"'a\\b'"


def test_quote_sql_literal_handles_combined_escapes():
    # backslash must be escaped before quotes, otherwise '\\\'' would be ambiguous
    assert quote_sql_literal("a\\'b") == "'a\\\\''b'"


def test_quote_sql_array_element_wraps_plain_value():
    assert quote_sql_array_element("EU") == "'EU'"


def test_quote_sql_array_element_backslash_escapes_single_quote():
    """Inside a CH array literal, single quotes are backslash-escaped
    (NOT doubled — that grammar is rejected inside `[...]`)."""
    assert quote_sql_array_element("O'Brien") == "'O\\'Brien'"


def test_quote_sql_array_element_doubles_backslash():
    assert quote_sql_array_element(r"a\b") == r"'a\\b'"


def test_quote_sql_array_element_handles_combined_escapes():
    # Backslash doubled, then single quote backslash-escaped.
    assert quote_sql_array_element("a\\'b") == "'a\\\\\\'b'"


def test_quote_sql_array_element_empty_string():
    assert quote_sql_array_element("") == "''"
```

- [ ] **Step 4.2: Run the new tests — they must fail with ImportError**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v 2>&1 | tail -8
```

Expected: `ImportError: cannot import name 'quote_sql_array_element' from 'iris.clickhouse.identifiers'` and `'quote_sql_literal'` (since `quote_string` still exists but the new names don't).

- [ ] **Step 4.3: Modify `src/iris/clickhouse/identifiers.py`**

Replace the existing `quote_string` function:

```
old:
def quote_string(value: str) -> str:
    """Quote a SQL string literal: backslashes are doubled, then single quotes are doubled."""
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"

new:
def quote_sql_literal(value: str) -> str:
    """Quote a SQL string literal for inline use in DDL or query text.

    Backslashes are doubled, then single quotes are doubled (CH's standard
    string-literal escape grammar). Use for values that appear directly in
    query text, e.g. row-policy USING clauses: ``USING col = 'value'``.

    Renamed from ``quote_string`` (atomic rename, no alias).
    """
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


def quote_sql_array_element(value: str) -> str:
    """Quote a SQL string for use as an element in a CH array literal.

    CH array literal syntax requires single-quoted string elements with
    backslash escaping (NOT doubled-quote escaping — that grammar is
    rejected inside ``[...]``). Backslashes are doubled, then single
    quotes are backslash-escaped. Use ONLY for values placed inside
    ``[...]`` array literals; for inline String literals use
    ``quote_sql_literal``.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"
```

- [ ] **Step 4.4: Update `src/iris/clickhouse/policies.py` — import + call sites + docstring**

```
old:
from iris.clickhouse.identifiers import (
    _FIXED_STRING_RE,
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)

new:
from iris.clickhouse.identifiers import (
    _FIXED_STRING_RE,
    policy_name,
    quote_identifier,
    quote_sql_literal,
    validate_identifier,
)
```

Update the two call sites in `_build_policy_filter` (around lines 178-179):

```
old:
        return f"has({col_q}, {quote_string(value)})"
    return f"{col_q} = {quote_string(value)}"

new:
        return f"has({col_q}, {quote_sql_literal(value)})"
    return f"{col_q} = {quote_sql_literal(value)}"
```

Update the docstring reference inside `_build_policy_filter` (around line 167):

```
old:
    string literal here via ``quote_string`` (regardless of branch,

new:
    string literal here via ``quote_sql_literal`` (regardless of branch,
```

- [ ] **Step 4.5: Update `src/iris/clickhouse/queries.py` — delegate the String/FixedString branch**

```
old:
from iris.clickhouse.identifiers import _FIXED_STRING_RE, quote_identifier

new:
from iris.clickhouse.identifiers import (
    _FIXED_STRING_RE,
    quote_identifier,
    quote_sql_array_element,
)
```

In `_marshal_array_element` (around lines 129-134):

```
old:
    if ch_type == "String" or _FIXED_STRING_RE.match(ch_type):
        if not isinstance(v, str):
            raise TypeError(f"{ch_type} expects str, got {type(v).__name__}")
        # Backslash first, then single quote — order matters.
        escaped = v.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"

new:
    if ch_type == "String" or _FIXED_STRING_RE.match(ch_type):
        if not isinstance(v, str):
            raise TypeError(f"{ch_type} expects str, got {type(v).__name__}")
        return quote_sql_array_element(v)
```

- [ ] **Step 4.6: Update `tests/clickhouse/test_clickhouse_policies.py` — fix the docstring reference**

```bash
grep -n "quote_string" tests/clickhouse/test_clickhouse_policies.py
```

Expected match at line 269. Edit:

```
old:
    """quote_string uses SQL-standard double-single-quote escaping; verify

new:
    """quote_sql_literal uses SQL-standard double-single-quote escaping; verify
```

- [ ] **Step 4.7: Verify no stale `quote_string` references remain anywhere**

```bash
grep -rn "quote_string" src/ tests/ docs/ CLAUDE.md 2>/dev/null
```

Expected: zero matches in `src/` and `tests/`. Doc files (`docs/clickhouse.md`, `CLAUDE.md`) still mention `quote_string` — those get updated in Task 6.

- [ ] **Step 4.8: Run the rename's downstream tests**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py tests/clickhouse/test_clickhouse_policies.py tests/clickhouse/test_query_marshaling.py -q
```

Expected: all green.

- [ ] **Step 4.9: Do NOT commit.**

---

## Task 5: `delete_database` orphan-grant sweep

**Files:**
- Modify: `src/iris/auth/views.py` — `DatabaseAdminSession.delete_database` body.
- Modify: `tests/clickhouse/test_admin_handle.py` — add the orphan-grant sweep test.

- [ ] **Step 5.1: Add the failing test**

Append to `tests/clickhouse/test_admin_handle.py`:

```python
def test_delete_database_revokes_orphan_grants_before_drop(ch_client, ch_settings, prefix):
    """U4: delete_database must REVOKE non-tier grants on the database
    before DROP DATABASE so re-creating with the same name doesn't
    reactivate orphan grants."""
    creator_username = f"{prefix}_creator"
    db = f"{prefix}_doomed_with_outsider"
    outsider_role = f"{prefix}_outsider"

    # Create the database via the normal creator path.
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator_username).create_database(db)
    )

    # Out-of-band: create a role and grant it SELECT on the database.
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{outsider_role}`")
    try:
        ch_client.command(f"GRANT SELECT ON `{db}`.* TO `{outsider_role}`")

        # Confirm the grant is present in system.grants.
        before = ch_client.query(
            "SELECT count() FROM system.grants WHERE database = {d:String} AND role_name = {r:String}",
            parameters={"d": db, "r": outsider_role},
        ).result_rows
        assert before[0][0] >= 1, "outsider grant not visible in system.grants pre-drop"

        # Run delete_database via the admin session.
        admin = _admin_session(ch_client, ch_settings, database=db, username=creator_username)
        asyncio.run(admin.delete_database())

        # Database should be gone.
        db_count = ch_client.query(
            "SELECT count() FROM system.databases WHERE name = {n:String}",
            parameters={"n": db},
        ).result_rows
        assert db_count[0][0] == 0, f"database {db} still present after delete"

        # Crucially: no surviving grant rows reference the dropped database.
        after = ch_client.query(
            "SELECT count() FROM system.grants WHERE database = {d:String}",
            parameters={"d": db},
        ).result_rows
        assert after[0][0] == 0, (
            f"orphan grants on {db} survived delete_database (count={after[0][0]})"
        )
    finally:
        ch_client.command(f"DROP ROLE IF EXISTS `{outsider_role}`")
```

(`_creator_session` and `_admin_session` are existing helpers in this file.)

- [ ] **Step 5.2: Run the new test — it must fail**

```bash
uv run pytest tests/clickhouse/test_admin_handle.py::test_delete_database_revokes_orphan_grants_before_drop -v 2>&1 | tail -10
```

Expected: AssertionError on `assert after[0][0] == 0` — the outsider's grant survives today's `delete_database`.

- [ ] **Step 5.3: Modify `src/iris/auth/views.py:DatabaseAdminSession.delete_database`**

```
old:
    async def delete_database(self) -> None:
        db_q = quote_identifier(self.database, kind="database")
        database = self.database
        client, _, _ = self._ch()

        def _sync() -> None:
            client.command(f"DROP DATABASE IF EXISTS {db_q}")
            drop_tier_roles(client, database=database)

        await asyncio.to_thread(_sync)

new:
    async def delete_database(self) -> None:
        db_q = quote_identifier(self.database, kind="database")
        database = self.database
        client, _, _ = self._ch()

        def _sync() -> None:
            # Sweep grants on this database before dropping. CH leaves
            # orphan rows in system.grants if a database is dropped while
            # grants reference it; re-creating with the same name
            # reactivates them. REVOKE ALL ON <db>.* per distinct grantee
            # is idempotent and uniform across user vs role grantees
            # (CH's REVOKE syntax accepts either).
            rows = client.query(
                """
                SELECT DISTINCT name FROM (
                    SELECT role_name AS name FROM system.grants
                    WHERE database = {d:String} AND role_name IS NOT NULL
                    UNION ALL
                    SELECT user_name AS name FROM system.grants
                    WHERE database = {d:String} AND user_name IS NOT NULL
                )
                """,
                parameters={"d": database},
            ).result_rows
            for (grantee,) in rows:
                grantee_q = quote_identifier(cast(str, grantee), kind="role")
                client.command(f"REVOKE ALL ON {db_q}.* FROM {grantee_q}")
            client.command(f"DROP DATABASE IF EXISTS {db_q}")
            drop_tier_roles(client, database=database)

        await asyncio.to_thread(_sync)
```

`cast` and `quote_identifier` are already imported in `views.py` (used by `list_admin_members` and the existing DDL); no new imports needed. Confirm with:

```bash
grep -n "^from typing\|cast\|quote_identifier" src/iris/auth/views.py | head -10
```

Expected: `cast` is in the `from typing import ...` line and `quote_identifier` is imported from `iris.clickhouse.identifiers`. If either is missing, add it.

- [ ] **Step 5.4: Run the new test plus the rest of test_admin_handle.py**

```bash
uv run pytest tests/clickhouse/test_admin_handle.py -v 2>&1 | tail -15
```

Expected: all tests pass, including the existing `test_delete_database_drops_tier_roles_and_db` and the new sweep test.

- [ ] **Step 5.5: Do NOT commit.**

---

## Task 6: Documentation updates

**Files:**
- Modify: `CLAUDE.md` — DDL-safety paragraph names the renamed helper.
- Modify: `docs/clickhouse.md` — identifier-quoting paragraph + module map block.
- Modify: `docs/operations.md` — short note about delete_database now sweeping grants.

- [ ] **Step 6.1: Update `CLAUDE.md` DDL-safety paragraph**

Find (around line 58):

```
old:
- **DDL safety**: external strings flow through `validate_identifier` + `quote_identifier` (`iris.clickhouse.identifiers`). Never f-string-concat raw user input into SQL. DML uses CH's `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.

new:
- **DDL safety**: external strings flow through `validate_identifier` + `quote_identifier` (`iris.clickhouse.identifiers`). For `kind` in `{database, username, group}`, `validate_identifier` also rejects names ending in iris's reserved role suffixes (`_USER`, `_GRP`, `_DBADMIN`, `_DBWRITER`, `_DBREADER`). String literals embedded in DDL use `quote_sql_literal` (inline literals) or `quote_sql_array_element` (CH array literal elements) — these have different escape grammars and the helper name picks the right one. DML uses CH's `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.
```

- [ ] **Step 6.2: Update `docs/clickhouse.md` identifier-quoting paragraph (around line 42)**

```
old:
`identifiers.py` is the single safety contract. External-source strings (usernames from auth, db/table/column names from callers) flow through `validate_identifier` (rejects anything outside `[a-zA-Z0-9_]+`) and `quote_identifier` (validates + backticks). Row-policy values use `quote_string` for SQL literal escaping. DDL is built from these helpers; `client.command()` runs it without parameter binding. DML (audit `SELECT`s) uses ClickHouse's native `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.

new:
`identifiers.py` is the single safety contract. External-source strings (usernames from auth, db/table/column names from callers) flow through `validate_identifier` (rejects anything outside `[a-zA-Z0-9_]+`; also rejects names ending in iris's reserved role suffixes — `_USER`, `_GRP`, `_DBADMIN`, `_DBWRITER`, `_DBREADER` — for `kind in {database, username, group}`) and `quote_identifier` (validates + backticks). Row-policy values use `quote_sql_literal` (CH inline literal grammar: doubled `''`); array literal elements use `quote_sql_array_element` (CH array-literal grammar: backslash-escaped `\'`). DDL is built from these helpers; `client.command()` runs it without parameter binding. DML (audit `SELECT`s) uses ClickHouse's native `{name:Type}` placeholder syntax via `client.query(..., parameters=...)`.
```

- [ ] **Step 6.3: Update `docs/clickhouse.md` module map (around line 145)**

```
old:
├── identifiers.py   # validate_identifier, quote_identifier, quote_string

new:
├── identifiers.py   # validate_identifier (with reserved-suffix block), quote_identifier,
│                    # quote_sql_literal, quote_sql_array_element, _FIXED_STRING_RE
```

- [ ] **Step 6.4: Add a `delete_database` sweep note to `docs/operations.md`**

Find the "Open security follow-ups" section. Append a new bullet (or insert appropriately near existing CH-related items):

```bash
grep -n "Open security follow-ups\|Out-of-band admin promotion" docs/operations.md
```

Locate the bullet list and add this line near the existing CH-related bullets (after the "Out-of-band admin promotion" one):

```
- **`delete_database` sweeps non-tier grants.** Before DROP DATABASE, iris now SELECTs distinct grantees from `system.grants WHERE database = ?` and runs `REVOKE ALL ON <db>.* FROM <grantee>` for each. Without this, CH leaves orphan grants in `system.grants` that reactivate if the database is recreated with the same name. Tier roles (`<db>_DBADMIN/_DBWRITER/_DBREADER`) are revoked first, then dropped. Closed by `docs/superpowers/specs/2026-05-09-sql-hygiene-design.md`.
```

- [ ] **Step 6.5: Verify the doc updates**

```bash
grep -n "quote_sql_literal\|quote_sql_array_element\|reserved iris role suffix\|reserved role suffix\|delete_database sweeps" CLAUDE.md docs/clickhouse.md docs/operations.md
```

Expected: at least 5 matches across the three files (one in CLAUDE.md, two in clickhouse.md, one or two in operations.md).

```bash
grep -rn "quote_string" src/ tests/ docs/ CLAUDE.md 2>/dev/null
```

Expected: zero matches anywhere — the rename is complete.

- [ ] **Step 6.6: Do NOT commit.**

---

## Task 7: Final verification + atomic commit

**Files:** none modified — verification + commit only.

- [ ] **Step 7.1: Run the full unit suite**

```bash
uv run --project /home/driou/dev/project/iris pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
```

Expected: all green. Total count = 402 (pre-spec) + new tests added across Tasks 2-5. Exact count is not asserted; the inventory diff in Step 7.5 is the regression check.

- [ ] **Step 7.2: Run ruff**

```bash
uv run --project /home/driou/dev/project/iris ruff check
```

Expected: zero warnings. (If pyflakes flags an unused `import re` in `policies.py` after Task 3's edit, remove the line and re-run.)

- [ ] **Step 7.3: Run basedpyright at error level (cheap fail-fast)**

```bash
uv run --project /home/driou/dev/project/iris basedpyright --level error
```

Expected: 0 errors.

- [ ] **Step 7.4: Run basedpyright at warning level (the merge gate per CLAUDE.md)**

```bash
uv run --project /home/driou/dev/project/iris basedpyright --level warning
```

Expected: 0 errors, 0 warnings.

- [ ] **Step 7.5: Test-inventory diff (no coverage regression)**

```bash
uv run --project /home/driou/dev/project/iris pytest --collect-only -q --ignore=tests/auth/integration --ignore=tests/clickhouse/integration > /tmp/pytest-inventory-after.txt
diff /tmp/pytest-inventory-before.txt /tmp/pytest-inventory-after.txt | head -50
```

Expected: only **additions** + the four `test_quote_string_*` → `test_quote_sql_literal_*` renames. New tests:

- `tests/clickhouse/test_clickhouse_identifiers.py` — 7 suffix-block tests (3 parameterized × 5 suffixes counts as 15 collected) + 1 `_FIXED_STRING_RE` sanity + 5 `quote_sql_array_element` tests + 1 reserved-suffix-error-message test = **17 distinct collected items beyond existing**, plus 4 quote_string→quote_sql_literal renames. Total: ~21 lines added in the diff.
- `tests/clickhouse/test_admin_handle.py` — 1 sweep test (+1 line).

The exact line count varies with parametrize expansion; the requirement is: only additions and renames, no removals.

- [ ] **Step 7.6: Run the auth-integration suite (Keycloak)**

```bash
uv run --project /home/driou/dev/project/iris pytest tests/auth/integration -q
```

Expected: 15 passed.

- [ ] **Step 7.7: Run the CH-integration suite (Keycloak + ClickHouse)**

```bash
uv run --project /home/driou/dev/project/iris pytest tests/clickhouse/integration -q
```

Expected: 8 passed.

- [ ] **Step 7.8: Review the full diff**

```bash
git -C /home/driou/dev/project/iris status --short
git -C /home/driou/dev/project/iris diff --stat
```

Expected: 8 modified files, no new files.
- src/iris/clickhouse/identifiers.py (3 fixes: suffix-block, regex hoist, two helpers added)
- src/iris/clickhouse/policies.py (regex import + helper rename)
- src/iris/clickhouse/queries.py (regex import + delegation to quote_sql_array_element)
- src/iris/auth/views.py (delete_database sweep)
- tests/clickhouse/test_clickhouse_identifiers.py (many new tests + renames)
- tests/clickhouse/test_admin_handle.py (one new test)
- tests/clickhouse/test_clickhouse_policies.py (one docstring update)
- CLAUDE.md, docs/clickhouse.md, docs/operations.md

- [ ] **Step 7.9: Stage everything**

```bash
git -C /home/driou/dev/project/iris add -A
git -C /home/driou/dev/project/iris status --short
```

Verify: every modified file is listed. No surprises (no `dist/`, `.coverage`, `.db`).

- [ ] **Step 7.10: Atomic commit**

```bash
git -C /home/driou/dev/project/iris commit -m "$(cat <<'EOF'
refactor(clickhouse): SQL/identifier hygiene — suffix-block, regex dedup, escape unification, delete_database grant sweep

Closes the four SQL/identifier hygiene findings from the 2026-05-09 review
per the spec at docs/superpowers/specs/2026-05-09-sql-hygiene-design.md
and the plan at docs/superpowers/plans/2026-05-09-sql-hygiene.md.

- validate_identifier rejects iris's reserved role suffixes (_USER,
  _GRP, _DBADMIN, _DBWRITER, _DBREADER) for kind in {database, username,
  group}. Tier role names like <db>_DBADMIN are exempt (kind=role).
- _FIXED_STRING_RE is now defined once in iris.clickhouse.identifiers
  and imported by both policies.py and queries.py (was duplicated).
- quote_string renamed to quote_sql_literal (atomic, no alias). New
  sibling quote_sql_array_element handles CH's array-literal escape
  grammar (backslash-escaped \\') so policies.py and queries.py no
  longer have inline divergent escape implementations.
- DatabaseAdminSession.delete_database now SELECTs distinct grantees
  from system.grants for the database and REVOKE ALL ON <db>.* per
  grantee before DROP DATABASE. CH leaves orphan grants otherwise.

After this spec, the original review's prioritized list is fully
closed (auth-reshape b11cf20, security-hardening 75a60ad, this).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7.11: Verify the commit landed**

```bash
git -C /home/driou/dev/project/iris log -1 --stat
git -C /home/driou/dev/project/iris status
```

Expected: HEAD is the new commit; working tree clean.

---

## Out of scope (do NOT touch)

After this spec, the original review's prioritized findings are fully closed. Items the user explicitly did not prioritize stay out of scope:

- Rate-limit-key salting / further DDoS hardening.
- OAuth nonce sliding window / JWKS refresh.
- DatabaseAdminSession's 12-method consolidation (grant/revoke pairs).
- N5/N6/N7 naming polish (`tier_role_name`, `iris_global_admin` casing, cookie-name prefixes).
- Anything not explicitly in the original review that surfaces during implementation — leave it for a future review pass.
