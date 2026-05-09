# SQL / identifier hygiene — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

Third and final follow-up spec from the 2026-05-09 review. The auth-reshape (`b11cf20`) and security-hardening (`75a60ad`) specs landed on `main`; file/symbol names below assume that layout.

This spec closes four review findings that all touch ClickHouse identifier handling and SQL DDL composition:

- **`validate_identifier` suffix-block** — names ending in iris's reserved role suffixes (`_USER`, `_GRP`, `_DBADMIN`, `_DBWRITER`, `_DBREADER`) can confuse the post-login role-graph walk in `derive_capabilities`. The walk recovers the database name from a tier role by stripping a tier suffix; an external-input name that already ends with one creates ambiguous parses. Defense-in-depth: validate at the boundary.
- **B5** — `_FIXED_STRING_RE = re.compile(r"^FixedString\(\d+\)$")` is defined twice, in `iris.clickhouse.policies` and `iris.clickhouse.queries`. The regex is identical today; drift is inevitable.
- **B6** — `iris.clickhouse.identifiers.quote_string` doubles single quotes (CH's standard literal escape grammar) while `iris.clickhouse.queries._marshal_array_element` backslash-escapes them (CH's array-element grammar). Both are correct in their context but the inconsistency is a foot-gun for anyone copying one helper to a new context.
- **U4** — `DatabaseAdminSession.delete_database` issues `DROP DATABASE IF EXISTS` and `drop_tier_roles` but does not revoke grants held by non-tier roles on the dropped database. CH leaves orphan rows in `system.grants`; recreating the database with the same name reactivates them.

The four items share a code surface (`iris.clickhouse.identifiers` plus the two callers) and benefit from one review window. Single PR, single commit at the end.

## Goal

Close the four hygiene findings as one cohesive pass:

1. Reject reserved suffixes in `validate_identifier` for `database`/`username`/`group` kinds.
2. Hoist `_FIXED_STRING_RE` to `iris.clickhouse.identifiers` and import it from both consumers.
3. Replace `quote_string` and the inline String-branch escape in `_marshal_array_element` with two clearly-named helpers `quote_sql_literal` and `quote_sql_array_element` (atomic rename, no backwards-compat alias).
4. `delete_database` sweeps grants on the database before the DROP.

## Non-goals

Anything not in the original review's prioritized list. After this spec, the review's prioritized findings are fully closed.

## Atomicity

Single commit on a feature branch. ruff + basedpyright (warning) + full pytest (incl. integration) green at merge.

---

## 1. File touch list

```
MODIFIED:
  src/iris/clickhouse/identifiers.py   # +_RESERVED_SUFFIXES, +_SUFFIX_CHECKED_KINDS,
                                       # validate_identifier rejects reserved suffixes
                                       # for the three external kinds. +_FIXED_STRING_RE
                                       # (hoisted). +quote_sql_literal (renamed from
                                       # quote_string). +quote_sql_array_element.
  src/iris/clickhouse/policies.py      # drop local _FIXED_STRING_RE; import from
                                       # identifiers; replace quote_string with
                                       # quote_sql_literal at the one call site.
  src/iris/clickhouse/queries.py       # drop local _FIXED_STRING_RE; import from
                                       # identifiers; _marshal_array_element's String
                                       # branch delegates to quote_sql_array_element.
  src/iris/auth/views.py               # DatabaseAdminSession.delete_database sweeps
                                       # grants on the database before DROP DATABASE.

  tests/clickhouse/test_clickhouse_identifiers.py
                                       # +tests for suffix-block, quote_sql_literal,
                                       # quote_sql_array_element, _FIXED_STRING_RE
                                       # contract. Existing quote_string tests get
                                       # renamed.
  tests/clickhouse/test_admin_handle.py
                                       # +test for delete_database orphan-grant sweep.

  CLAUDE.md                            # update DDL-safety conventions section to
                                       # mention the new helpers and the suffix-block.
  docs/operations.md                   # short note: delete_database now sweeps grants.
  docs/clickhouse.md                   # update identifier-quoting paragraph.
```

No new files.

---

## 2. Component-by-component design

### 2.1 — Suffix-block in `validate_identifier`

`src/iris/clickhouse/identifiers.py`. Add reserved-suffix rejection driven by an opt-in `kind` allowlist:

```python
_RESERVED_SUFFIXES: Final = ("_USER", "_GRP", "_DBADMIN", "_DBWRITER", "_DBREADER")
_SUFFIX_CHECKED_KINDS: Final = frozenset({"database", "username", "group"})


def validate_identifier(name: str, *, kind: str) -> str:
    """Reject anything outside ``[a-zA-Z0-9_]+``. Returns ``name`` unchanged on success.

    For ``kind`` in ``{"database", "username", "group"}``, additionally rejects names
    ending in iris's reserved role suffixes (``_USER``, ``_GRP``, ``_DBADMIN``,
    ``_DBWRITER``, ``_DBREADER``). These suffixes are reserved for synthesized role
    names (e.g. ``<username>_USER``, ``<database>_DBADMIN``); allowing external input
    to also end with them creates ambiguity in the post-login role-graph walk in
    ``iris.clickhouse.capabilities.derive_capabilities``.

    Other ``kind`` values (``role``, ``policy``, ``table``, ``column``) skip the
    suffix check, since synthesized role names like ``<db>_DBADMIN`` legitimately
    end in those suffixes.

    ``kind`` is woven into the error message ("username", "role", "database", ...) so
    operators tracing a bad input can see where it entered.
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

`quote_identifier` calls `validate_identifier` so the rejection propagates without further changes at call sites.

**Implication for `provision_user`** (`src/iris/clickhouse/users.py`): the function already runs `validate_identifier(username, kind="username")` and per-group `validate_identifier(group, kind="group")`. After this change, a login with a username/group ending in a reserved suffix fails loudly at provisioning time — before any session row is written. That is the intended defense.

### 2.2 — `_FIXED_STRING_RE` hoisting

Add to `src/iris/clickhouse/identifiers.py`:

```python
# CH's FixedString(N) type marker. Used by row-policy filter construction
# and the typed param marshaller to detect FixedString variants of array
# element types.
_FIXED_STRING_RE: Final = re.compile(r"^FixedString\(\d+\)$")
```

Replace the local definitions in `policies.py:21` and `queries.py:60` with imports:

```python
from iris.clickhouse.identifiers import _FIXED_STRING_RE
```

The leading underscore stays — the regex is package-private, not part of the public surface re-exported by `iris.clickhouse.__init__`.

### 2.3 — Escape-grammar unification

Two new helpers in `identifiers.py`. `quote_string` is renamed outright (no alias) per the project's atomic-rename convention.

```python
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

Call-site updates:

- `src/iris/clickhouse/policies.py:_build_policy_filter` — `quote_string(value)` → `quote_sql_literal(value)`.
- `src/iris/clickhouse/queries.py:_marshal_array_element` — its String / FixedString branch's inline `escaped = v.replace(...)` block becomes `return quote_sql_array_element(v)`.

### 2.4 — `delete_database` grant sweep (U4)

`src/iris/auth/views.py:DatabaseAdminSession.delete_database`. Replace the `_sync` body so it sweeps grants before the DROP:

```python
async def delete_database(self) -> None:
    db_q = quote_identifier(self.database, kind="database")
    database = self.database
    client, _, _ = self._ch()

    def _sync() -> None:
        # Sweep grants on this database before dropping. CH leaves orphan
        # rows in system.grants if a database is dropped while grants
        # reference it; recreating with the same name reactivates them.
        # REVOKE ALL ON <db>.* per distinct grantee is idempotent and
        # uniform across user vs role grantees (CH's REVOKE syntax accepts
        # either).
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

`cast` and `quote_identifier` are already imported in `views.py` (used by `list_admin_members` and the existing DDL).

**Edge case — non-iris-format grantee names.** A grantee whose name contains characters outside `[a-zA-Z0-9_]+` (e.g. an out-of-band operator created a role with special characters and granted it on the database) raises `InvalidIdentifierError` at `quote_identifier`. The exception propagates to the route layer; the database is NOT dropped. Operator gets a loud error and can clean up the offending grant manually. Acceptable for v0.1.0 — iris-managed databases shouldn't have such grants in practice.

**Tier roles.** `<db>_DBADMIN`/`_DBWRITER`/`_DBREADER` themselves hold grants on the database, so they appear in the sweep. Their grants are revoked, then `drop_tier_roles` drops the roles entirely. Order is correct.

**Row policies.** CH's `DROP DATABASE` cascades to `system.row_policies` rows referencing the database (CH 24+ behavior). The existing `test_delete_database_drops_tier_roles_and_db` already covers this implicitly; no separate sweep needed.

---

## 3. Migration & deployment

No env-var changes. No SQLite migration. No CH state migration.

**Operator-facing impact:** an existing CH installation may contain databases whose names end in a reserved suffix or users/groups with such names. After this change, `provision_user` (post-login) and any path that calls `validate_identifier` on those identifiers will start raising `InvalidIdentifierError`. In practice this should be a no-op for any iris deployment that followed the documented identity matching (`docs/auth.md` § Identity matching) — the reserved suffixes are iris-internal conventions, not natural identifier endings.

A short note in `docs/operations.md` calls this out as a precondition: operators upgrading should `SELECT name FROM system.databases / system.users / system.roles WHERE name LIKE '%\_USER' OR name LIKE '%\_GRP' OR name LIKE '%\_DBADMIN' OR name LIKE '%\_DBWRITER' OR name LIKE '%\_DBREADER' ESCAPE '\\'` (filtering out tier roles) and rename any survivors before deploying.

---

## 4. Risk acceptance

What this spec does NOT close:

- **Suffix-block at the validation boundary, not at the CH side.** A CH role created out-of-band still bypasses the check. The suffix-block defends against iris-driven creates only. Operators who manage CH users/roles directly are responsible for not colliding with iris's namespace.
- **`delete_database` sweep is best-effort if CH grants are altered concurrently.** Between the SELECT in the sweep and the REVOKE per grantee, another connection could have added a new grant. The newly-added grant survives the sweep. Acceptable: iris's session model is single-admin-per-database for the delete operation, and concurrent grant-modification during deletion is an out-of-band operator action.
- **The escape helpers are CH-specific.** They handle CH's lexer rules (doubled `''` for inline literals, backslash for array elements). They are not safe for other SQL dialects.

---

## 5. Testing strategy

Each fix gets at least one new test. Existing tests (402 unit + 23 integration after the security-hardening spec) must stay green.

### 5.1 — Suffix-block in `validate_identifier`

`tests/clickhouse/test_clickhouse_identifiers.py`. Add:

- `test_validate_identifier_rejects_reserved_suffix_for_database` — for each of the 5 suffixes, `validate_identifier(f"foo{suffix}", kind="database")` raises `InvalidIdentifierError`.
- `test_validate_identifier_rejects_reserved_suffix_for_username` — same, with `kind="username"`.
- `test_validate_identifier_rejects_reserved_suffix_for_group` — same, with `kind="group"`.
- `test_validate_identifier_accepts_reserved_suffix_for_role` — `validate_identifier("mydb_DBADMIN", kind="role")` passes (tier role names legitimately end in those suffixes).
- `test_validate_identifier_accepts_reserved_suffix_for_table_column_policy` — same for `kind in {"table", "column", "policy"}`.
- `test_validate_identifier_accepts_normal_names` — `validate_identifier("alice", kind="username")` and `validate_identifier("sales", kind="group")` pass.
- `test_validate_identifier_error_message_mentions_suffix` — error text includes the offending suffix so operators tracing logs can see why.

### 5.2 — `_FIXED_STRING_RE` hoisting

No new behavior — pure refactor. Existing tests in `test_clickhouse_policies.py` and `test_query_marshaling.py` exercise the FixedString paths. The merge gate catches any forgotten import.

Add one trivial sanity test:

- `test_fixed_string_re_matches_expected_forms` — asserts `_FIXED_STRING_RE.match("FixedString(16)")` is non-None and `_FIXED_STRING_RE.match("String")` is None. Documents the regex's contract at its new home.

### 5.3 — Escape-grammar unification

`tests/clickhouse/test_clickhouse_identifiers.py`. Replace the existing `quote_string` tests with `quote_sql_literal` tests (mechanical rename of the function name in each test) and add `quote_sql_array_element` tests:

- `test_quote_sql_literal_doubles_single_quote` — `quote_sql_literal("O'Brien")` returns `"'O''Brien'"`.
- `test_quote_sql_literal_doubles_backslash` — `quote_sql_literal("path\\file")` returns `"'path\\\\file'"`.
- `test_quote_sql_literal_handles_backslash_then_quote` — `quote_sql_literal("\\'")` returns the form CH parses back to `\'` (backslash doubled first, then quote doubled).
- `test_quote_sql_array_element_backslash_escapes_quote` — `quote_sql_array_element("O'Brien")` returns `"'O\\'Brien'"`.
- `test_quote_sql_array_element_doubles_backslash` — `quote_sql_array_element("path\\file")` returns `"'path\\\\file'"`.

### 5.4 — `delete_database` grant sweep

`tests/clickhouse/test_admin_handle.py`. Add:

- `test_delete_database_revokes_orphan_grants_before_drop` —
  1. Create database `db` via `DatabaseCreatorSession.create_database`.
  2. Manually `CREATE ROLE outsider; GRANT SELECT ON db.* TO outsider`.
  3. Verify the grant exists in `system.grants WHERE database = db AND role_name = 'outsider'`.
  4. `await admin.delete_database()`.
  5. Assert `system.grants WHERE database = db` returns zero rows.
  6. Cleanup: `DROP ROLE IF EXISTS outsider`.

This test uses the existing `ch_client`, `ch_settings`, `prefix` fixtures (CH testcontainer in the non-integration tier).

### 5.5 — Gates

1. `uv run ruff check` — zero warnings.
2. `uv run basedpyright --level error` — zero errors.
3. `uv run basedpyright --level warning` — zero warnings (merge gate per CLAUDE.md).
4. `uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration` — green; total count = pre-spec baseline + new tests. Inventory diff confirms zero coverage regression.
5. `uv run pytest tests/auth/integration` — green (Docker required).
6. `uv run pytest tests/clickhouse/integration` — green (Docker required).

Pyright will catch any forgotten reference to the renamed `quote_string`.

---

## 6. Out of scope

This is the third and final follow-up spec from the 2026-05-09 review. Items the user explicitly did not prioritize stay out of scope:

- Rate-limit-key salting / further DDoS hardening.
- OAuth nonce sliding window / JWKS refresh.
- `DatabaseAdminSession`'s 12-method consolidation (grant/revoke pairs).
- N5/N6/N7 naming polish (`tier_role_name`, `iris_global_admin` casing, cookie-name prefixes).
- Anything not explicitly in the original review that surfaces during implementation — leave it for a future review pass.

After this spec, the original review's prioritized list is fully closed.
