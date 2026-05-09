# ClickHouse end-to-end integration tests — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

Iris has unit-style CH integration tests under `tests/clickhouse/`
(testcontainer-backed, but bypassing the auth pipeline) and OAuth
integration tests under `tests/auth/integration/` (real Keycloak, but
scoped to the auth flow only). There is no test that exercises the
full chain end-to-end: real Keycloak → real iris auth flow → real
session subclasses → real ClickHouse → row-policy enforcement.

The session-method API today (`DatabaseAdminSession.grant_writer`,
`add_row_policy`, `query_as_user`, `AdminSession.user_grants`, etc.)
is what future HTTP routes will call. Verifying it end-to-end now
catches integration bugs before route surface lands.

## Goals

- Cover every iris tier role with an authenticated user from real
  Keycloak: global-admin, db-creator, db-writer, db-reader.
- Provision iris's group → role mapping through the real post-login
  hook (`init_user_rights` + `derive_rights`).
- Verify row-level policies actually filter: the same SELECT issued
  by different users returns different rows because of the policy.
- Cover representative admin operations: audit reads, revokes,
  database deletion.
- Land the suite in a folder that's skippable from dev runs without
  modifying the suite itself.

## Non-goals

- Adding new HTTP routes (`/api/grant`, `/api/policies`, etc.). The
  iris API surface as it stands is the session-method layer; tests
  drive it directly. Future HTTP routes are their own design exercise.
- Concurrent-login load testing.
- Long-running policy correctness (e.g., adding policy mid-query).
- DROPping CH entities between tests beyond what `prefix`-based
  naming gives us (testcontainer is session-bounded; state
  accumulates within a session, which is fine).
- Modifying alice/bob/`admins`/`users` entries in the existing realm
  seed — additions only.

---

## Test scenario (canonical setup chain)

The chain every test file uses, in order:

1. **alice logs in** via simulated Keycloak flow.
   - Iris's post-login hook runs `init_user_rights(alice, [admins, users])`,
     creates `alice_USER`, `admins_GRP`, `users_GRP`, grants
     `IMPERSONATE ON alice TO iris_svc`.
   - `derive_rights` flags `is_admin=True` because `admins_GRP` (created
     by `bootstrap_admin` from `CLICKHOUSE_ADMIN_GROUP=admins`) holds
     `ROLE ADMIN WGO` at global scope.
2. **alice grants CREATE DATABASE to `creators_GRP`** via
   `query_as_service` (once per session, via fixture).
   ```python
   await alice.query_as_service("CREATE ROLE IF NOT EXISTS creators_GRP")
   await alice.query_as_service("GRANT CREATE DATABASE ON *.* TO creators_GRP")
   ```
3. **bob logs in** → `derive_rights` returns `can_create_database=True`
   (because bob's `creators_GRP` membership is now visible to
   `system.grants` with `access_type='CREATE DATABASE'`,
   `database IS NULL`).
4. **bob creates `test_db_<prefix>`** via
   `DatabaseCreatorSession.create_database`. The implementation
   auto-grants bob DBADMIN.
5. **bob, now DBADMIN, creates the many-typed records table** via
   `query_as_service` (or via the underlying client; bob is admin of
   his own database).
6. **bob grants DBWRITER to `writers_GRP`** and **DBREADER to
   `readers_GRP`**.
7. **carol logs in** → `derive_rights` flags
   `db_writer={'test_db_<prefix>'}` because `writers_GRP` is in her
   role set and DBWRITER is granted to it.
8. **carol inserts rows** via `DatabaseSession.query_as_user` against
   the writer-tier session.
9. **dave logs in** → `derive_rights` flags `db_reader={…}`. Initially
   he sees all rows.
10. **alice adds the row policy** `has(tags, 'EU') TO readers_GRP` via
    `AdminSession.add_row_policy`.
11. **dave queries** → CH's row-policy machinery filters; dave sees
    only EU-tagged rows.
12. **alice queries via `query_as_service`** → no row-policy chain,
    sees all rows. (The policy's USING-1 wildcard for
    `iris_global_admin` would also let alice-as-user see all rows;
    the integration test prefers `query_as_service` for clarity.)

---

## Realm seed extension

File: `tests/auth/integration/seed/keycloak-realm.json`.

**Additions only**:

- New users:
  - `carol` (password: `carol-pw`), groups: `users`, `writers`.
  - `dave` (password: `dave-pw`), groups: `users`, `readers`.
- New groups: `creators`, `writers`, `readers`.
- bob's groups list grows to include `creators`. (Existing tests
  assert bob is in `users` and not admin; adding `creators` does not
  break those assertions.)

**Unchanged**: alice (admins, users), bob's `users` membership,
`admins` group, `users` group. The 12 existing
`tests/auth/integration/test_oauth_integration.py` tests continue to
pass with no edits.

---

## Fixture promotion

Both `tests/auth/integration/` and `tests/clickhouse/integration/`
need the same Keycloak container, the same TLS material, and the same
realm. The cleanest factoring is:

- Move `keycloak_container` and `tls_paths` fixtures from
  `tests/auth/integration/conftest.py` to a new top-level
  `tests/integration/conftest.py` *(rejected: pytest doesn't
  auto-discover sibling conftests)*. **Use** `tests/conftest.py`
  (top-level) — pytest ascends from each test file looking for
  conftest.py, so a top-level one is visible everywhere.
- Move the `_tls.py` helper alongside.
- Update the realm seed path inside `keycloak_container` to use a
  resolved-from-this-conftest base.

Result: `tests/conftest.py` owns Keycloak + TLS;
`tests/auth/integration/conftest.py` owns the auth-specific
`oauth_app` + `keycloak_http`; new
`tests/clickhouse/integration/conftest.py` owns the CH-specific
`iris_app` + per-user login helpers.

---

## File map

| File | Change |
|---|---|
| `tests/conftest.py` | Add `keycloak_container` and `tls_paths` (moved from `tests/auth/integration/conftest.py`). |
| `tests/_tls.py` | Move from `tests/auth/integration/_tls.py`. Existing import `from tests.auth.integration._tls import …` rewrites to `from tests._tls import …`. |
| `tests/seed/keycloak-realm.json` | Move from `tests/auth/integration/seed/keycloak-realm.json` AND extend with carol/dave/creators/writers/readers. |
| `tests/auth/integration/conftest.py` | Drop the moved fixtures; keep `oauth_app` and `keycloak_http`. Update realm path. |
| `tests/auth/integration/_keycloak_helpers.py` | No change required (uses passed-in `test_client` and `http`). |
| `tests/auth/integration/test_oauth_integration.py` | Update import: `from tests._tls import TLSPaths`. |
| `tests/auth/integration/test_integration_tls.py` | Same import update if applicable. |
| `tests/clickhouse/integration/conftest.py` *(new)* | `iris_app` per-test fixture (build with `install_clickhouse=True`), session-scoped `provisioned_grants` autouse fixture (alice + grant CREATE DATABASE to creators_GRP). |
| `tests/clickhouse/integration/_helpers.py` *(new)* | `login_as(test_client, keycloak_http, username, password)` → sid (drives the Keycloak flow); `session_for(app, sid, *, kind, database=None)` → constructs a typed Session subclass from the stored `UserSession`. |
| `tests/clickhouse/integration/test_creator_flow.py` *(new)* | bob creates DB + table; dave attempts → AuthForbidden. |
| `tests/clickhouse/integration/test_writer_flow.py` *(new)* | carol inserts; dave attempts insert via writer dep → AuthForbidden. |
| `tests/clickhouse/integration/test_row_policies.py` *(new)* | Policy filters dave; alice unfiltered via `query_as_service`. |
| `tests/clickhouse/integration/test_admin_flow.py` *(new)* | Audit reads, list operations. |
| `tests/clickhouse/integration/test_revoke_flow.py` *(new)* | Revoke writer; delete database. |
| `CLAUDE.md` | Add a one-line note alongside the existing skip instruction documenting the new integration suite path. |

---

## Test specifications

### `test_creator_flow.py`

Two tests:

- **`test_creator_can_create_database_and_table`** — bob logs in, his
  derived rights show `can_create_database=True`, calls
  `create_database("test_db_<prefix>")`, then creates the records
  table via the resulting DBADMIN session. Asserts the database and
  the table exist in `system.databases` and `system.columns`.
- **`test_non_creator_cannot_create_database`** — dave logs in, his
  rights have `can_create_database=False`. Calling
  `session_for(..., kind='database_creator')` raises
  `iris.auth.exceptions.AuthForbidden` (because the dep gates on
  `is_admin or can_create_database`). The test verifies the AuthForbidden
  is raised at session-construction time, not at create-time.

### `test_writer_flow.py`

- **`test_writer_can_insert_rows`** — bob's setup creates the
  database and grants writer to `writers_GRP`. carol logs in,
  obtains a `DatabaseSession` (writer tier), inserts 4 rows via
  `query_as_user` (the impersonated path). Asserts `SELECT count()`
  via `query_as_user` returns 4.
- **`test_reader_cannot_insert`** — dave's session resolves at
  `kind='database_writer'` → `AuthForbidden` (his rights don't have
  `db_writer={test_db}`).

### `test_row_policies.py`

Single load-bearing test:

- **`test_row_policy_filters_reader_but_not_admin`** — full chain:
  bob creates DB and table; carol inserts 4 rows (2 EU, 2 US); alice
  adds `has(tags, 'EU') TO readers_GRP`; dave (reader) queries via
  `query_as_user` → 2 rows; alice queries via `query_as_service` → 4
  rows. Asserts the row IDs explicitly so a future regression that
  flips the filter direction would fail.

### `test_admin_flow.py`

- **`test_admin_audit_queries_return_consistent_state`** — after the
  full chain, alice runs `user_grants(username='carol')`,
  `role_grants(role='writers_GRP')`, `user_role_memberships`,
  `table_row_policies`, `list_admin_members` (now from the new
  `DatabaseAdminSession.list_admin_members` — returns
  `[{kind, name}, ...]`). Verifies the role chain is consistent.

### `test_revoke_flow.py`

- **`test_revoke_writer_drops_writer_rights_on_next_login`** —
  starting from the chain, bob calls `revoke_writer_from_group('writers')`.
  carol logs back in (NEW session); her derived rights no longer
  include `db_writer={test_db}`. Asserting via her whoami response.
- **`test_delete_database_drops_db_and_tier_roles`** — bob calls
  `delete_database()`. Verifies `system.databases` no longer has
  `test_db`, and `system.roles` no longer has `test_db_DB*`.

---

## Helpers

### `tests/clickhouse/integration/_helpers.py`

```python
"""End-to-end integration test helpers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris.auth.identity import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
)
from iris.auth.exceptions import AuthForbidden
from iris.auth.sessions import UserSession  # for typing only
from tests.auth.integration._keycloak_helpers import simulate_login


SessionKind = Literal[
    "auth",
    "admin",
    "database_creator",
    "database_admin",
    "database_writer",
    "database_reader",
]


async def login_as(
    *, test_client: TestClient, http: httpx.Client, username: str, password: str
) -> str:
    """Drive the full Keycloak login flow for ``username``; return the iris_session sid."""
    response = simulate_login(
        test_client=test_client, http=http, username=username, password=password,
    )
    sid = response.cookies.get("iris_session")
    assert sid is not None, f"login for {username} did not set iris_session"
    return sid


async def session_for(
    app: FastAPI,
    sid: str,
    *,
    kind: SessionKind,
    database: str | None = None,
) -> AuthSession:
    """Reconstitute a typed Session subclass from the stored UserSession.

    Mirrors what iris.auth.deps does inside an HTTP request, but callable
    from test bodies. Raises ``AuthForbidden`` from the same code path the
    real deps would raise from when the user lacks the required rights.
    """
    store = app.state.auth_session_store
    stored = await store.get_and_refresh(sid)
    assert stored is not None, f"session {sid!r} not in store (logged out?)"

    refs = (
        getattr(app.state, "clickhouse_client", None),
        getattr(app.state, "clickhouse_http_client", None),
        getattr(app.state, "clickhouse_settings", None),
    )
    common = {
        "id": stored.id, "user": stored.user,
        "created_at": stored.created_at, "expires_at": stored.expires_at,
        "data": stored.data, "rights": stored.rights,
        "client": refs[0], "http_client": refs[1],
        "settings": refs[2], "store": store,
    }

    rights = stored.rights
    if kind == "auth":
        return AuthSession(**common)
    if kind == "admin":
        if not rights.is_admin:
            raise AuthForbidden(needed=("admin",), have=())
        return AdminSession(**common)
    if kind == "database_creator":
        if not (rights.is_admin or rights.can_create_database):
            raise AuthForbidden(needed=("admin", "database_creator"), have=())
        return DatabaseCreatorSession(**common)
    assert database is not None, f"kind={kind} requires database="
    if kind == "database_admin":
        if not rights.has_admin(database):
            raise AuthForbidden(needed=(f"database_admin[{database}]",), have=())
        return DatabaseAdminSession(**common, database=database)
    if kind == "database_writer":
        if not rights.has_write(database):
            raise AuthForbidden(needed=(f"database_writer[{database}]",), have=())
        return DatabaseSession(**common, database=database)
    if kind == "database_reader":
        if not rights.has_read(database):
            raise AuthForbidden(needed=(f"database_reader[{database}]",), have=())
        return DatabaseSession(**common, database=database)
    raise ValueError(f"unknown kind: {kind}")
```

This helper exists in tests-only space; it duplicates the gating
logic from `iris.auth.deps` deliberately so the test layer doesn't
import private resolvers.

### `tests/clickhouse/integration/conftest.py`

```python
"""Fixtures for ClickHouse end-to-end integration tests."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def iris_app(monkeypatch, ch_settings, keycloak_container, tls_paths) -> FastAPI:
    """A fresh iris app with install_clickhouse=True for each test.

    Depends on ``ch_settings`` from ``tests/clickhouse/conftest.py``,
    which already sets the CLICKHOUSE_* env vars pointing at the
    testcontainer. We layer the auth + admin-group env vars on top.

    AUTH_METHOD=oauth pointed at the real Keycloak; CLICKHOUSE_ADMIN_GROUP=
    admins so alice's rights derive to is_admin=True on first login.
    """
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OIDC_ISSUER_URL", keycloak_container.issuer_url)
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "iris-test-secret")
    monkeypatch.setenv("OIDC_SCOPES", "openid profile email")
    monkeypatch.setenv("OIDC_CA_CERT_PATH", str(tls_paths.ca_pem))
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_ADMIN_GROUP", "admins")

    from iris.app import build_app
    return build_app(install_clickhouse=True)


@pytest.fixture
def keycloak_http(tls_paths):
    """Reused from tests/auth/integration; copied for visibility here."""
    import ssl
    ctx = ssl.create_default_context(cafile=str(tls_paths.ca_pem))
    with httpx.Client(verify=ctx, follow_redirects=True, timeout=10.0) as client:
        yield client
```

Note: the implementation plan must reconcile the CH fixture chain.
`iris_app` needs the CH testcontainer host/port set in env, which is
exactly what `ch_settings` (from `tests/clickhouse/conftest.py`) does.
The plan's Task 2 wires this together explicitly — the spec just
notes the dependency.

---

## Skippability and discoverability

- Skip during dev:
  ```
  uv run pytest --ignore=tests/clickhouse/integration --ignore=tests/auth/integration
  ```
- Skip via marker is NOT used (existing pattern is folder-based).
- CLAUDE.md gets a single-line update in the testing section noting
  the new path.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Fixture promotion breaks `tests/auth/integration` | Implementation plan has a phase that runs the full auth integration suite after the move. Zero changes to test bodies; only import paths shift. |
| Realm seed edits break alice/bob tests | Spec mandates additions only. The implementation plan diff-checks the realm JSON to confirm alice/bob/admins/users entries are byte-identical. |
| Initial test run pays Keycloak + CH boot ≈45s | Both containers are session-scoped; subsequent tests reuse them. Target: 5 integration tests run in <60s after warm start. |
| Per-user provisioning races on first concurrent login | iris's `init_user_rights` uses `IF NOT EXISTS` everywhere; CH RBAC ops are idempotent. Tests run sequentially within a file; no inter-test parallelism. |
| Test ordering for setup chain | Each test file owns its full chain (alice → bob → carol → dave). Tests are independent; no cross-file dependencies. The session-scoped `provisioned_grants` fixture handles the once-per-session `GRANT CREATE DATABASE` so it doesn't repeat. |
| Tests accumulate CH state (databases, users, roles) within session | Acceptable: session-scoped containers drop everything when pytest exits. Per-test `prefix` prevents same-session collisions. |
| `session_for` helper drift from `iris.auth.deps` | Both branch on the same Rights flags; if iris's gating changes, the helper needs updating. Mitigated by integration tests failing loudly if gating drifts (the same `AuthForbidden` is raised). Documented in the helper docstring. |

---

## Out of scope (deferred)

- New HTTP routes (`/api/grant`, `/api/policies`, etc.). Their own spec.
- Tests for `DatabaseCreatorSession` operations beyond `create_database`
  (none exist today).
- Concurrency / race tests on row-policy creation.
- Performance / benchmarking.
