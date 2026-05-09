# Authorization service — remove private-session access; restore the authz boundary

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

`src/iris/features/authorization/service.py` contains five functions that reach into `session._ch()` to grab the raw ClickHouse client and run queries directly:

| Line | Function | What it does |
|---|---|---|
| 56 | `list_members` | tier-role membership for a database |
| 93 | `list_all_users` | every CH user + their granted roles |
| 120 | `list_all_databases` | every CH database with admin/writer/reader counts |
| 146 | `list_all_row_policies` | every row policy across the cluster |
| 157 | `list_all_grants` | every row in `system.grants` |

Each one bypasses the typed `XxxSession` boundary and runs as the iris service identity (`iris_svc`, which holds `iris_global_admin` via the bootstrap). The dep gate in front of these handlers (`SessionAdmin` / `SessionDatabaseAdmin`) is therefore the *only* line of defense; the moment someone accidentally accepts `Session` instead, the queries still run with full privilege. **This is a security regression, not a stylistic one.**

The audit run before writing this spec found these five and **no others**: every other module in `src/` respects the typed-session boundary. The damage is contained to one file added during the frontend-architecture work.

## Goal

Move the five SQL bodies into `iris.clickhouse.{audit, grants}`, expose them as async methods on the appropriate `XxxSession` subclass, and rewrite `service.py` to consume the typed methods. Restore the invariant: **`src/` contains zero `# pyright: ignore[reportPrivateUsage]` and zero `# noqa: SLF001` suppressions.**

## Non-goals

- **Changing the typed-session inheritance hierarchy.** AuthSession → DatabaseSession → DatabaseAdminSession (and the DatabaseCreatorSession, AdminSession siblings) stay as-is; we just add methods.
- **Changing the existing `iris.clickhouse.audit` API for per-user / per-role / per-table queries.** `user_grants`, `role_grants`, `user_role_memberships`, `user_row_policies`, `role_row_policies`, `table_row_policies` are unchanged. Four new top-level inventory queries (`list_all_users`, `list_all_databases`, `list_all_row_policies`, `list_all_grants`) get added alongside.
- **Changing routes.py.** The action and render routes already use the right typed deps (from the typed-deps + per-intent refactors). Only the service layer and its consumers shift.
- **Changing how tests cover the violation.** Existing tests monkeypatch `iris.features.authorization.service.list_*`; after the fix they monkeypatch the typed session methods directly (`iris.auth.views.AdminSession.list_users`, etc.). Same coverage, correct mocking surface.

## 1. CH SQL helper homes

| Helper | Module | Signature |
|---|---|---|
| `list_tier_members` | `iris.clickhouse.grants` | `(client, *, database: str) -> dict[str, list[dict[str, str]]]` — returns `{"admin": [...], "reader": [...], "writer": [...]}`; each entry is `{"kind": "user" \| "role", "name": <str>}` |
| `list_all_users` | `iris.clickhouse.audit` | `(client) -> list[dict[str, Any]]` — `[{"name": <username>, "groups": [<role_name>, …]}]` |
| `list_all_databases` | `iris.clickhouse.audit` | `(client) -> list[dict[str, Any]]` — `[{"name": <db>, "admin_count": int, "writer_count": int, "reader_count": int}]` |
| `list_all_row_policies` | `iris.clickhouse.audit` | `(client) -> list[dict[str, Any]]` — full rows from `system.row_policies` |
| `list_all_grants` | `iris.clickhouse.audit` | `(client) -> list[dict[str, Any]]` — full rows from `system.grants` |

Helpers are sync; the session methods wrap them via `asyncio.to_thread`. Same shape as the existing `audit.user_grants`, `policies.add_row_policy`, `grants.create_tier_roles`, etc.

`list_tier_members` lives in `grants.py` because it's about iris's tier roles (uses `tier_role_name(database, tier)` to know what to query for). The four `list_all_*` are pure read-only system-table queries with no iris-specific knowledge — they belong in `audit.py`.

## 2. Session method surface

### `DatabaseAdminSession`

Replaces `list_admin_members()` with a richer `list_members()`:

```python
async def list_members(self) -> dict[str, list[dict[str, str]]]:
    """Return tier-role members for self.database, keyed by tier:
    {"admin": [...], "reader": [...], "writer": [...]}.

    Each entry is {"kind": "user" | "role", "name": <str>}.
    """
    client, _, _ = self._ch()
    return await asyncio.to_thread(
        grants.list_tier_members, client, database=self.database,
    )
```

The old `list_admin_members()` is **deleted**. After the refactor, no caller needs admin-only — every caller (Authorization UI's manage page) renders all three tiers. Net change: -1 method, +1 method, simpler API.

### `AdminSession`

Four new methods, each one-line `asyncio.to_thread(audit.<helper>, client)`:

```python
async def list_users(self) -> list[dict[str, Any]]:
    client, _, _ = self._ch()
    return await asyncio.to_thread(audit.list_all_users, client)

async def list_databases(self) -> list[dict[str, Any]]:
    client, _, _ = self._ch()
    return await asyncio.to_thread(audit.list_all_databases, client)

async def list_all_row_policies(self) -> list[dict[str, Any]]:
    client, _, _ = self._ch()
    return await asyncio.to_thread(audit.list_all_row_policies, client)

async def list_all_grants(self) -> list[dict[str, Any]]:
    client, _, _ = self._ch()
    return await asyncio.to_thread(audit.list_all_grants, client)
```

Naming: `list_users` and `list_databases` get the short form (no per-X variant exists). `list_all_row_policies` and `list_all_grants` get the `list_all_` prefix to disambiguate from the existing per-user / per-role / per-table methods (`user_row_policies`, `role_row_policies`, `table_row_policies`, `user_grants`, `role_grants`).

## 3. `service.py` after the fix

`service.py` becomes a thin aggregator over typed-session methods. No `_ch()` calls, no SLF001/reportPrivateUsage suppressions, no direct CH knowledge.

```python
"""Read-side helpers for the Authorization feature.

Pure functions that take typed sessions and return template-ready dicts.
No ClickHouse access — that's behind the XxxSession methods.
"""
from __future__ import annotations

from typing import Any

from iris.auth.rights import Capabilities
from iris.auth.views import AdminSession, DatabaseAdminSession


def my_access_view(caps: Capabilities) -> dict[str, Any]:
    """Build the template context for the my_access render."""
    return {
        "reader_dbs": sorted(caps.db_reader),
        "writer_dbs": sorted(caps.db_writer),
        "admin_dbs": sorted(caps.db_admin),
        "can_create_database": caps.can_create_database,
        "is_admin": caps.is_admin,
    }


async def manage_view(session: DatabaseAdminSession) -> dict[str, Any]:
    """Build the manage-page context."""
    members = await session.list_members()
    row_policies = await session.list_row_policies()
    audit = await session.list_grants()
    return {
        "members": members,
        "row_policies": row_policies,
        "audit": audit,
    }
```

The four `list_all_*` aggregator functions in service.py go away entirely. The admin-console sub-tab routes call the typed `AdminSession` methods directly (no service.py wrapper needed).

## 4. Routes

The five admin-console routes in `routes.py` switch from `await list_all_*(admin)` to `await admin.list_*()`:

```python
# Before
from iris.features.authorization.service import list_all_users
users = await list_all_users(admin)

# After
users = await admin.list_users()
```

Same shape for `list_databases`, `list_all_row_policies`, `list_all_grants`. Five edits across `routes.py`. The `from iris.features.authorization.service import …` lines for those names are removed (only `my_access_view` and `manage_view` remain in service.py and they're still imported from intents/routes).

## 5. CLAUDE.md addition

Insert under the existing **## Conventions** section, near the other patterns:

```markdown
- **Don't access private fields across module boundaries.** A name with
  a leading underscore (`_field`, `_method`) is private to the module
  that defines it. Reaching into `obj._field` from another module — or
  adding `# pyright: ignore[reportPrivateUsage]` / `# noqa: SLF001` to
  suppress the warning — is forbidden in `src/`. If you need the
  functionality, propose a helper function (or method) on the owning
  module that exposes it through a proper public API.

  The suppression comment is the smell, not the fix. Tests are exempt
  by config (basedpyright + ruff disable both checks for `tests/`);
  the rule applies to `src/`.

  **In iris specifically**, the `XxxSession` hierarchy in
  `iris.auth.views` IS the authorization boundary; reaching into
  `session._ch()` from a feature module bypasses the entire tier
  model and is a security violation. If a feature needs CH access,
  add a sync SQL helper in `iris.clickhouse.<module>` and wrap it as
  an async method on the right `XxxSession` subclass (`AdminSession`
  for global, `DatabaseAdminSession` for per-database). Routes and
  service code consume the typed method.
```

## 6. Tests

### New CH helper tests (against the testcontainer)

One unit test per CH helper, in the appropriate `tests/clickhouse/test_clickhouse_<module>.py`:

- `tests/clickhouse/test_clickhouse_grants.py` — extend with `test_list_tier_members_returns_three_tier_dict`. Sets up admin/reader/writer tier roles for a prefix-namespaced database, grants users and groups to each, asserts the returned dict shape.
- `tests/clickhouse/test_clickhouse_audit.py` — extend with `test_list_all_users_returns_users_with_role_lists`, `test_list_all_databases_returns_tier_counts`, `test_list_all_row_policies_includes_seeded_policy`, `test_list_all_grants_includes_seeded_grant`.

Each test creates the relevant CH state under a `prefix`-namespaced entity and asserts the helper sees it. Same testcontainer pattern as the existing `audit.user_grants` / `policies.add_row_policy` tests.

### Session-method tests (mock-based)

Add `tests/auth/test_admin_inventory_methods.py`:

- `test_list_users_calls_audit_helper` — monkeypatch `iris.auth.views.audit.list_all_users`, instantiate AdminSession with mocks, await `admin.list_users()`, assert the helper was called with the client from `_ch()`.
- Same shape for `list_databases`, `list_all_row_policies`, `list_all_grants`.

Add `tests/auth/test_database_admin_list_members.py` (or extend an existing `tests/auth/test_database_admin_*.py`):

- `test_list_members_calls_grants_helper` — monkeypatch `iris.auth.views.grants.list_tier_members`, instantiate DatabaseAdminSession, await `db.list_members()`, assert kwargs include `database=self.database`.

### Updated existing tests

`tests/features/test_authorization_admin_console.py` and `tests/features/test_authorization_audit.py` currently monkeypatch `iris.features.authorization.service.list_all_*` and `iris.features.authorization.service.list_members`. After the refactor, those names are gone. Update each monkeypatch to target the typed session method (e.g. `iris.auth.views.AdminSession.list_users` and `iris.auth.views.DatabaseAdminSession.list_members`).

`tests/features/test_authorization_manage.py` similarly monkeypatches `iris.features.authorization.service.manage_view` in one test (`test_manage_render_renders_database_name`); that one is unchanged since `manage_view` survives in service.py (it's the only function left).

The deprecated `list_admin_members` method on `DatabaseAdminSession` is removed; any existing test that referenced it (none confirmed by grep, but verify) gets rewritten against `list_members`.

## 7. Final audit step (after the refactor lands)

The implementation plan ends with an explicit audit task that fails the merge if any private-access leak slipped in. Three checks, each must produce zero matches in `src/`:

```bash
# 1. No reportPrivateUsage suppressions in src/
grep -rn "reportPrivateUsage" src/

# 2. No noqa: SLF001 in src/ (ruff's analog of the same rule)
grep -rn "SLF001" src/

# 3. No cross-module dunder-prefixed access on session-like names. Spot-check
#    via grep for the obvious surfaces; manual review for anything that
#    survives.
grep -rn "session\._\|admin\._\|creator\._\|db\._\|db_session\._" src/
```

Expected: all three return zero results. If any survives, the refactor is not done.

This audit task is part of the implementation plan; running it is a hard precondition for the final commit.

## 8. Files

| Path | Change |
|---|---|
| `src/iris/clickhouse/grants.py` | Add `list_tier_members(client, *, database)` |
| `src/iris/clickhouse/audit.py` | Add `list_all_users`, `list_all_databases`, `list_all_row_policies`, `list_all_grants` (4 new sync helpers) |
| `src/iris/auth/views.py` | DatabaseAdminSession: replace `list_admin_members()` with `list_members()`. AdminSession: add `list_users()`, `list_databases()`, `list_all_row_policies()`, `list_all_grants()` |
| `src/iris/features/authorization/service.py` | Delete the 5 `_ch()`-using functions; `manage_view` becomes a thin aggregator over `session.list_members()` / `session.list_row_policies()` / `session.list_grants()` |
| `src/iris/features/authorization/routes.py` | Five admin-console routes: `await list_all_users(admin)` → `await admin.list_users()` (and equivalents); drop the corresponding `from iris.features.authorization.service import list_all_*` lines |
| `tests/clickhouse/test_clickhouse_grants.py` | Add `test_list_tier_members_returns_three_tier_dict` |
| `tests/clickhouse/test_clickhouse_audit.py` | Add 4 tests, one per new helper |
| `tests/auth/test_admin_inventory_methods.py` (new) | 4 mock-based tests for new AdminSession methods |
| `tests/auth/test_database_admin_list_members.py` (new) | 1 mock-based test for DatabaseAdminSession.list_members |
| `tests/features/test_authorization_admin_console.py` | Update 4 monkeypatches: `iris.features.authorization.service.list_all_*` → `iris.auth.views.AdminSession.list_*` |
| `tests/features/test_authorization_audit.py` | Update monkeypatch for `list_members` |
| `CLAUDE.md` | Add the convention bullet |

## 9. Risks and tradeoffs

- **`list_admin_members` removal.** No production callers other than service.py (which is being rewritten); any test that references it (none confirmed; verify) gets updated. Acceptable.
- **`list_all_grants` returns the entire `system.grants` table.** With many tenants, this could be tens of thousands of rows. Currently no pagination — same as the existing service.py implementation, so this isn't a regression. Add `LIMIT` / pagination later if needed; out of scope here.
- **Schema dependence on `system.users` / `system.role_grants` / `system.row_policies` / `system.grants`.** All four are CH-internal system tables; their schemas are stable across the supported CH 26.x line. Change tracking happens via integration tests.
- **The "final audit step" relies on grep heuristics.** It catches the obvious cases (reportPrivateUsage, SLF001, common session-variable names). A determined wrong-pattern with creative naming could slip through; the deeper safeguard is the convention in CLAUDE.md plus the feedback memory + any future code reviews.
