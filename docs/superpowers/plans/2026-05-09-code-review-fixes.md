# Code-Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply 13 review findings as 4 sequenced phases (docs, security/correctness, OIDC hardening, service-locator typing) on the iris codebase, each landing as one or more atomic commits.

**Architecture:** Phase 1 is documentation-only / one-line fixes. Phase 2 is local security patches with no API surface change. Phase 3 reworks OIDC identity and discovery in `oauth.py` (lazy async-safe, not lifespan-eager). Phase 4 tightens session typing without behavior change.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, Datastar, clickhouse-connect, httpx, pyjwt, ldap3, pytest + testcontainers (real CH + Keycloak).

**Spec:** `docs/superpowers/specs/2026-05-09-code-review-fixes-design.md`.

**Conventions you must respect:**
- DDL safety: external strings flow through `validate_identifier` + `quote_identifier`. Never f-string-concat raw user input into SQL. DML uses `{name:Type}` placeholder syntax with `client.query(..., parameters=...)`.
- Tests live under `tests/` (sibling to `src/`), no `__init__.py` under `tests/`, every test file basename is unique.
- ClickHouse tests use a real testcontainer (session-scoped); per-test isolation via the `prefix` fixture.
- Lint gate: `uv run ruff check` must produce zero warnings.
- Type gate: `uv run basedpyright --level error` and `--level warning` must both stay at zero.
- Test gate: `uv run pytest` must be green.

---

## Phase 1 — Documentation & no-op cleanups

Five tasks; each one tiny commit. No new tests required.

### Task 1: Add `from __future__ import annotations` to 5 files

**Files:**
- Modify: `src/iris/__init__.py`
- Modify: `src/iris/app.py`
- Modify: `src/iris/templates.py`
- Modify: `src/iris/auth/__init__.py`
- Modify: `src/iris/clickhouse/__init__.py`

- [ ] **Step 1: Add the import as the first non-blank line of each file**

For each file above, place `from __future__ import annotations` immediately under any module docstring (or as line 1 if no docstring), followed by one blank line. Other existing imports follow.

Example for `src/iris/templates.py`:

```python
"""Shared `Jinja2Templates` instance for both root-level (`index.html`)
and auth-flow (`auth/*.html`) templates. Imported by `iris.app:build_app`
and re-exposed on `app.state.templates` so exception handlers and providers
can render without re-creating the loader.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
```

- [ ] **Step 2: Verify the gates stay green**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`
Expected: zero ruff warnings, zero pyright errors, all tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/iris/__init__.py src/iris/app.py src/iris/templates.py src/iris/auth/__init__.py src/iris/clickhouse/__init__.py
git commit -m "style: add __future__ annotations import to remaining modules"
```

---

### Task 2: Remove stale module references in 3 files

The `iris.clickhouse.handle` module and the `CLICKHOUSE_SERVICE_ADMIN_USER` env var were both removed in earlier commits. Three docstrings still reference them.

**Files:**
- Modify: `src/iris/clickhouse/__init__.py:1-13` (the module docstring)
- Modify: `src/iris/clickhouse/install.py:1-9` (the module docstring)
- Modify: `src/iris/clickhouse/users.py:60-65` (the inline comment above the IMPERSONATE grant)

- [ ] **Step 1: Edit `src/iris/clickhouse/__init__.py` module docstring**

Replace the existing docstring (lines 1-13) with:

```python
"""ClickHouse provisioning, audit helpers, and per-tier ops.

Public surface — see ``CLAUDE.md`` for usage. Session subclasses in
``iris.auth.identity`` call into these helpers via ``asyncio.to_thread``.

The ``install`` function lives in ``iris.clickhouse.install`` but is *not*
re-exported from this package: callers (only ``iris.app:build_app``) do
``from iris.clickhouse.install import install``. Removing it from this
``__init__`` breaks an old module-load cycle where importing the package
triggered loading ``iris.auth.bootstrap`` via ``install``.
"""
```

- [ ] **Step 2: Edit `src/iris/clickhouse/install.py` module docstring**

Replace lines 1-9 with:

```python
"""Wire iris.clickhouse into a FastAPI app.

Builds the shared clickhouse-connect Client and a shared httpx.AsyncClient
for impersonated queries (``EXECUTE AS`` cannot use clickhouse-connect's
binary protocol, so user-scoped queries go through the HTTP endpoint
directly), runs the CH-side bootstrap (creates iris_global_admin sentinel
plus optional admin user/group roles from CLICKHOUSE_ADMIN_USER /
CLICKHOUSE_ADMIN_GROUP), stashes everything on app.state, and registers a
post-login provisioning hook so init_user_rights + derive_rights run once
per real authentication.
"""
```

- [ ] **Step 3: Edit `src/iris/clickhouse/users.py` inline comment**

Find the block around line 60-64:

```python
    # The IMPERSONATE grantee is the CH user iris connects as. After dropping
    # CLICKHOUSE_SERVICE_ADMIN_USER, that's just settings.user.
    impersonator_q = quote_identifier(settings.user, kind="user")
    client.command(f"GRANT IMPERSONATE ON {user_q} TO {impersonator_q}")
```

Replace with:

```python
    # The IMPERSONATE grantee is the CH user iris connects as
    # (settings.user). All HTTP queries-as-user route through this identity.
    impersonator_q = quote_identifier(settings.user, kind="user")
    client.command(f"GRANT IMPERSONATE ON {user_q} TO {impersonator_q}")
```

- [ ] **Step 4: Verify gates stay green**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`

- [ ] **Step 5: Commit**

```bash
git add src/iris/clickhouse/__init__.py src/iris/clickhouse/install.py src/iris/clickhouse/users.py
git commit -m "docs(clickhouse): remove stale references to deleted handle module + service-admin env var"
```

---

### Task 3: Prune redundant docstrings in `grants.py`

Five functions in `src/iris/clickhouse/grants.py` end their one-line docstring with "Idempotent." after restating the function name. The `_ensure_role` enumeration-defense rationale and the WHY-comments stay.

**Files:**
- Modify: `src/iris/clickhouse/grants.py:63-107, 110-121`

- [ ] **Step 1: Trim the five docstrings**

Apply these exact replacements:

`grant_tier_to_user` docstring becomes:
```python
    """``GRANT <database>_<tier> TO <username>_USER``.

    Pre-creates the user role if it does not yet exist (closes a username
    enumeration channel via differential CH errors).
    """
```

`grant_tier_to_group` docstring becomes:
```python
    """``GRANT <database>_<tier> TO <group>_GRP``.

    Pre-creates the group role if it does not yet exist.
    """
```

`revoke_tier_from_user` docstring becomes:
```python
    """``REVOKE <database>_<tier> FROM <username>_USER``."""
```

`revoke_tier_from_group` docstring becomes:
```python
    """``REVOKE <database>_<tier> FROM <group>_GRP``."""
```

`grant_select_to_database` docstring becomes:
```python
    """``GRANT SELECT ON <database>.* TO <role>``."""
```

`revoke_select_from_database` docstring becomes:
```python
    """``REVOKE SELECT ON <database>.* FROM <role>``."""
```

`grant_insert_update_to_table` docstring becomes:
```python
    """``GRANT INSERT`` and ``GRANT ALTER UPDATE`` on ``<database>.<table>`` to ``<role>``."""
```

Leave `_ensure_role`, `create_tier_roles`, `drop_tier_roles`, and `tier_role_name` docstrings unchanged.

- [ ] **Step 2: Verify gates stay green**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x tests/clickhouse/test_clickhouse_grants.py`

- [ ] **Step 3: Commit**

```bash
git add src/iris/clickhouse/grants.py
git commit -m "docs(grants): drop redundant 'Idempotent.' filler from public docstrings"
```

---

### Task 4: Collapse dead `if rights_json` branch in `_row_to_session`

The schema says `rights_json TEXT NOT NULL DEFAULT '{}'`. The existing fallback branch is unreachable.

**Files:**
- Modify: `src/iris/auth/sessions.py:63-80`

- [ ] **Step 1: Replace the `_row_to_session` body**

Replace lines 63-80 with:

```python
def _row_to_session(row: sqlite3.Row) -> UserSession:
    user = User(
        subject=row["subject"],
        username=row["username"],
        display_name=row["display_name"],
        groups=tuple(json.loads(row["groups_json"])),
    )
    rights = rights_from_dict(json.loads(row["rights_json"]))
    return UserSession(
        id=row["id"],
        user=user,
        created_at=_from_ts(row["created_at_ts"]),
        expires_at=_from_ts(row["expires_at_ts"]),
        absolute_expires_at=_from_ts(row["absolute_expires_at_ts"]),
        data=json.loads(row["data_json"]),
        rights=rights,
    )
```

The `EMPTY_RIGHTS` import becomes unused; remove it from the `from iris.auth.session import` line on line 35:

```python
from iris.auth.session import Rights, rights_from_dict, rights_to_dict
```

- [ ] **Step 2: Verify gates stay green**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x tests/auth/test_session_store.py`
Expected: all session-store tests still pass (the dead branch was never exercised).

- [ ] **Step 3: Commit**

```bash
git add src/iris/auth/sessions.py
git commit -m "refactor(sessions): drop unreachable rights_json branch in _row_to_session"
```

---

### Task 5: Add `Allow: GET` header to login POST 405 response

**Files:**
- Modify: `src/iris/auth/routes.py:109-110`

- [ ] **Step 1: Update the 405 response**

Replace:

```python
        if not isinstance(provider, (LDAPProvider, MockProvider)):
            return Response(status_code=405)
```

With:

```python
        if not isinstance(provider, (LDAPProvider, MockProvider)):
            return Response(status_code=405, headers={"Allow": "GET"})
```

- [ ] **Step 2: Add a test in a new file**

Create `tests/auth/test_login_method_not_allowed.py`:

```python
"""When AUTH_METHOD=oauth, POST /login is not allowed; the 405 response
must include an ``Allow`` header per RFC 7231 §6.5.5.
"""
import pytest
from fastapi.testclient import TestClient


def test_login_post_returns_405_with_allow_header(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://kc.example/realms/iris")
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("AUTH_DB_PATH", ":memory:")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    from iris.app import build_app

    app = build_app(install_clickhouse=False)
    with TestClient(app) as client:
        response = client.post("/login", data={"username": "x", "password": "y"})
    assert response.status_code == 405
    assert response.headers.get("Allow") == "GET"
```

- [ ] **Step 3: Run the new test**

Run: `uv run pytest tests/auth/test_login_method_not_allowed.py -v`
Expected: PASS.

- [ ] **Step 4: Verify the rest of the suite stays green**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`

- [ ] **Step 5: Commit**

```bash
git add src/iris/auth/routes.py tests/auth/test_login_method_not_allowed.py
git commit -m "fix(auth): include Allow: GET on login POST 405 (RFC 7231)"
```

---

## Phase 2 — Targeted security & correctness fixes

Five tasks. Each lands a security/correctness fix with its own test.

### Task 6: Drop `_ensure_role` from revoke paths

`revoke_tier_from_user` / `revoke_tier_from_group` currently pre-create the principal role before issuing `REVOKE`. That pattern is correct on grant (closes username enumeration via differential CH errors) but on revoke it leaks state for any unknown principal an attacker submits.

**Files:**
- Modify: `src/iris/clickhouse/grants.py:87-107`
- Test: `tests/clickhouse/test_clickhouse_grants.py`

- [ ] **Step 1: Write the failing test in `tests/clickhouse/test_clickhouse_grants.py`**

Append at the bottom of that file:

```python
def test_revoke_tier_from_user_does_not_create_role(ch_client, prefix):
    """Revoke must not pre-create the user-role for an unknown username:
    that would leak state for any value an attacker submits.
    """
    from iris.clickhouse.grants import (
        TIER_DBREADER,
        create_tier_roles,
        revoke_tier_from_user,
    )

    db = f"{prefix}_revoke_no_leak"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    nonexistent_user = f"{prefix}_ghost"

    revoke_tier_from_user(
        ch_client, database=db, tier=TIER_DBREADER, username=nonexistent_user
    )

    rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name = {n:String}",
        parameters={"n": f"{nonexistent_user}_USER"},
    ).result_rows
    assert rows[0][0] == 0, (
        f"revoke must not have created role {nonexistent_user}_USER"
    )


def test_revoke_tier_from_group_does_not_create_role(ch_client, prefix):
    from iris.clickhouse.grants import (
        TIER_DBREADER,
        create_tier_roles,
        revoke_tier_from_group,
    )

    db = f"{prefix}_revoke_no_leak_grp"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    nonexistent_group = f"{prefix}_ghost_grp"

    revoke_tier_from_group(
        ch_client, database=db, tier=TIER_DBREADER, group=nonexistent_group
    )

    rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name = {n:String}",
        parameters={"n": f"{nonexistent_group}_GRP"},
    ).result_rows
    assert rows[0][0] == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/clickhouse/test_clickhouse_grants.py::test_revoke_tier_from_user_does_not_create_role -v`
Expected: FAIL — the role is created by `_ensure_role`.

- [ ] **Step 3: Patch `revoke_tier_from_user`**

In `src/iris/clickhouse/grants.py`, replace:

```python
def revoke_tier_from_user(
    client: Client, *, database: str, tier: str, username: str
) -> None:
    """``REVOKE <database>_<tier> FROM <username>_USER``."""
    user_role = f"{username}{USER_ROLE_SUFFIX}"
    _ensure_role(client, user_role)
    user_role_q = quote_identifier(user_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    client.command(f"REVOKE {tier_q} FROM {user_role_q}")
```

with:

```python
def revoke_tier_from_user(
    client: Client, *, database: str, tier: str, username: str
) -> None:
    """``REVOKE <database>_<tier> FROM <username>_USER``.

    Does NOT pre-create the user-role: revoke must not leak state for
    arbitrary attacker-supplied usernames. CH no-ops on a missing role.
    """
    user_role = f"{username}{USER_ROLE_SUFFIX}"
    validate_identifier(user_role, kind="role")
    user_role_q = quote_identifier(user_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    try:
        client.command(f"REVOKE {tier_q} FROM {user_role_q}")
    except DatabaseError as err:
        if "Role" in str(err) and "not found" in str(err):
            return
        raise
```

- [ ] **Step 4: Patch `revoke_tier_from_group` symmetrically**

```python
def revoke_tier_from_group(
    client: Client, *, database: str, tier: str, group: str
) -> None:
    """``REVOKE <database>_<tier> FROM <group>_GRP``.

    Does NOT pre-create the group-role; CH no-ops on a missing role.
    """
    group_role = f"{group}{GROUP_ROLE_SUFFIX}"
    validate_identifier(group_role, kind="role")
    group_role_q = quote_identifier(group_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    try:
        client.command(f"REVOKE {tier_q} FROM {group_role_q}")
    except DatabaseError as err:
        if "Role" in str(err) and "not found" in str(err):
            return
        raise
```

Add the import at the top of `grants.py`:

```python
from clickhouse_connect.driver.exceptions import DatabaseError

from iris.clickhouse.identifiers import quote_identifier, validate_identifier
```

(`validate_identifier` may already be imported via `quote_identifier`'s call site — check; if so, expose it on the import line.)

- [ ] **Step 5: Run all grants tests**

Run: `uv run pytest tests/clickhouse/test_clickhouse_grants.py -v`
Expected: all pass, including the two new tests.

- [ ] **Step 6: Commit**

```bash
git add src/iris/clickhouse/grants.py tests/clickhouse/test_clickhouse_grants.py
git commit -m "fix(grants): revoke must not pre-create the principal role"
```

---

### Task 7: Wrap `_get_and_refresh_sync` in `BEGIN IMMEDIATE` transaction

The existing `SELECT … then UPDATE/DELETE` window is a TOCTOU under multiple uvicorn workers sharing the SQLite WAL.

**Files:**
- Modify: `src/iris/auth/sessions.py:184-214`

- [ ] **Step 1: Replace the body of `_get_and_refresh_sync`**

```python
    def _get_and_refresh_sync(self, session_id: str) -> UserSession | None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                self._conn.execute("COMMIT")
                return None
            now = datetime.now(UTC)
            expires_at = _from_ts(row["expires_at_ts"])
            absolute_expires_at = _from_ts(row["absolute_expires_at_ts"])
            if expires_at <= now or absolute_expires_at <= now:
                self._conn.execute(
                    "DELETE FROM sessions WHERE id = ?", (session_id,)
                )
                self._conn.execute("COMMIT")
                return None
            new_expires = now + self._ttl
            self._conn.execute(
                "UPDATE sessions SET expires_at_ts = ? WHERE id = ?",
                (_to_ts(new_expires), session_id),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        session = _row_to_session(row)
        return UserSession(
            id=session.id,
            user=session.user,
            created_at=session.created_at,
            expires_at=new_expires,
            absolute_expires_at=session.absolute_expires_at,
            data=session.data,
            rights=session.rights,
        )
```

- [ ] **Step 2: Run existing session-store tests**

Run: `uv run pytest tests/auth/test_session_store.py tests/auth/test_session_store_multiprocess.py -v`
Expected: all pass unchanged. (`BEGIN IMMEDIATE` is invisible at the API surface.)

- [ ] **Step 3: Verify gates**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`

- [ ] **Step 4: Commit**

```bash
git add src/iris/auth/sessions.py
git commit -m "fix(sessions): wrap _get_and_refresh_sync in BEGIN IMMEDIATE"
```

---

### Task 8: CSRF cookie sanity check

`mint_csrf_token` reuses whatever string is in the `iris_csrf` cookie. A malformed/attacker-supplied value persists for an hour. Add a length+charset gate.

**Files:**
- Modify: `src/iris/auth/csrf.py`
- Test: `tests/auth/test_csrf.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/auth/test_csrf.py`:

```python
import re

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from iris.auth.csrf import CSRF_COOKIE_NAME, mint_csrf_token


def _echo_app() -> FastAPI:
    app = FastAPI()

    @app.get("/echo")
    async def echo(request: Request) -> dict[str, str]:
        return {"token": mint_csrf_token(request)}

    return app


def test_mint_csrf_token_replaces_malformed_cookie():
    """An attacker-controlled or garbage cookie value must not be reused;
    mint_csrf_token replaces it with a fresh secrets.token_urlsafe(32).
    """
    client = TestClient(_echo_app())
    response = client.get(
        "/echo", cookies={CSRF_COOKIE_NAME: "../../../etc/passwd"}
    )
    token = response.json()["token"]
    assert token != "../../../etc/passwd"
    assert re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token), token


def test_mint_csrf_token_reuses_well_formed_cookie():
    """A well-formed urlsafe-base64 token of acceptable length is reused."""
    fixed = "A" * 32  # 32 chars, urlsafe-base64 charset
    client = TestClient(_echo_app())
    response = client.get("/echo", cookies={CSRF_COOKIE_NAME: fixed})
    assert response.json()["token"] == fixed
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/auth/test_csrf.py::test_mint_csrf_token_replaces_malformed_cookie -v`
Expected: FAIL (current implementation reuses any value).

- [ ] **Step 3: Patch `mint_csrf_token`**

In `src/iris/auth/csrf.py`, replace lines 1-15 with:

```python
from __future__ import annotations

import hmac
import re
import secrets

from fastapi import Form, HTTPException, Request, Response

CSRF_COOKIE_NAME = "iris_csrf"
CSRF_FORM_FIELD = "_csrf_token"

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def mint_csrf_token(request: Request) -> str:
    """Return the CSRF token: reuse the cookie value if well-formed, else mint fresh.

    A well-formed token matches ``[A-Za-z0-9_-]{32,128}`` (urlsafe-base64,
    minimum entropy of ``secrets.token_urlsafe(24)``). Anything else is
    treated as untrusted and replaced with a fresh ``token_urlsafe(32)``.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME, "")
    if existing and _TOKEN_RE.fullmatch(existing):
        return existing
    return secrets.token_urlsafe(32)
```

- [ ] **Step 4: Re-run the new tests**

Run: `uv run pytest tests/auth/test_csrf.py -v`
Expected: all pass.

- [ ] **Step 5: Verify gates**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/csrf.py tests/auth/test_csrf.py
git commit -m "fix(csrf): reject malformed cookie values, mint fresh token instead"
```

---

### Task 9: Type-aware CH parameter marshaler in `query_as_user`

`str(v)` works for str/int/float but corrupts `bool` (`"True"` instead of `"1"`) and serializes `datetime` with the timezone suffix CH may reject. Add a small marshaller that handles the right types and rejects the rest with a clear `TypeError`.

**Files:**
- Modify: `src/iris/clickhouse/queries.py`
- Test: `tests/clickhouse/test_query_marshaling.py` (new)

- [ ] **Step 1: Write the failing test in a new file `tests/clickhouse/test_query_marshaling.py`**

```python
"""Unit tests for the private CH HTTP-param marshaller."""
from datetime import UTC, datetime

import pytest


def _import_marshal():
    from iris.clickhouse.queries import _marshal_param

    return _marshal_param


def test_marshal_bool_true_is_one():
    m = _import_marshal()
    assert m(True) == "1"


def test_marshal_bool_false_is_zero():
    m = _import_marshal()
    assert m(False) == "0"


def test_marshal_int_passes_through():
    m = _import_marshal()
    assert m(42) == "42"


def test_marshal_float_passes_through():
    m = _import_marshal()
    assert m(3.14) == "3.14"


def test_marshal_str_passes_through():
    m = _import_marshal()
    assert m("hello") == "hello"


def test_marshal_datetime_iso_no_tz_suffix():
    m = _import_marshal()
    dt = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    assert m(dt) == "2026-05-09T12:00:00"


def test_marshal_none_raises():
    m = _import_marshal()
    with pytest.raises(TypeError, match="unsupported CH param type: NoneType"):
        m(None)


def test_marshal_list_raises():
    m = _import_marshal()
    with pytest.raises(TypeError, match="unsupported CH param type: list"):
        m([1, 2, 3])
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest tests/clickhouse/test_query_marshaling.py -v`
Expected: FAIL — `_marshal_param` does not exist yet.

- [ ] **Step 3: Add `_marshal_param` and use it in `query_as_user`**

In `src/iris/clickhouse/queries.py`, add the helper above `query_as_user`:

```python
from datetime import datetime


def _marshal_param(v: object) -> str:
    """Marshal a Python value for CH's HTTP ``param_<name>`` query string.

    CH's ``{name:Type}`` placeholders apply server-side type conversion, so
    we hand it a string. ``bool`` must be checked before ``int`` (Python
    ``bool`` subclasses ``int``); ``datetime`` is rendered without a tz
    suffix so CH parses it as ``DateTime``.
    """
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float, str)):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds").replace("+00:00", "")
    raise TypeError(f"unsupported CH param type: {type(v).__name__}")
```

Then in `query_as_user`, replace:

```python
    if parameters:
        for k, v in parameters.items():
            params[f"param_{k}"] = str(v)
```

with:

```python
    if parameters:
        for k, v in parameters.items():
            params[f"param_{k}"] = _marshal_param(v)
```

- [ ] **Step 4: Run unit + integration tests**

Run: `uv run pytest tests/clickhouse/test_query_marshaling.py -v`
Expected: all pass.

Run: `uv run pytest tests/clickhouse/ -x`
Expected: existing CH tests still pass — `query_as_user` is exercised through the per-tier session methods.

- [ ] **Step 5: Verify gates**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`

- [ ] **Step 6: Commit**

```bash
git add src/iris/clickhouse/queries.py tests/clickhouse/test_query_marshaling.py
git commit -m "fix(clickhouse): type-aware param marshaller for HTTP query_as_user"
```

---

### Task 10: `list_admin_members` returns users + roles

The current query selects only `role_name` from `system.role_grants`, so users granted the admin role directly are invisible. Reshape to return `[{"kind": "user"|"role", "name": ...}, ...]`.

**Files:**
- Modify: `src/iris/auth/identity.py:214-225`
- Test: `tests/clickhouse/test_admin_handle.py:113-122`

- [ ] **Step 1: Update the existing test**

In `tests/clickhouse/test_admin_handle.py`, replace lines 113-122 with:

```python
def test_list_admin_members_returns_creator(ch_client, ch_settings, prefix):
    creator = f"{prefix}_c"
    db = f"{prefix}_members"
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)
    members = asyncio.run(admin.list_admin_members())
    # Creator is granted DBADMIN to its user-role (not directly to the user
    # account), so the entry is kind="role" with the per-user role name.
    assert {"kind": "role", "name": f"{creator}_USER"} in members


def test_list_admin_members_includes_direct_user_grant(
    ch_client, ch_settings, prefix
):
    """A user account granted the admin role directly (not via _USER role)
    appears with kind='user'."""
    creator = f"{prefix}_c2"
    db = f"{prefix}_members2"
    direct_user = f"{prefix}_direct"

    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    # Create a CH user account and grant the admin role directly.
    ch_client.command(
        f"CREATE USER IF NOT EXISTS `{direct_user}` IDENTIFIED WITH no_password"
    )
    ch_client.command(f"GRANT `{db}_DBADMIN` TO `{direct_user}`")

    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)
    members = asyncio.run(admin.list_admin_members())
    assert {"kind": "user", "name": direct_user} in members
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/clickhouse/test_admin_handle.py::test_list_admin_members_returns_creator tests/clickhouse/test_admin_handle.py::test_list_admin_members_includes_direct_user_grant -v`
Expected: both fail (return shape mismatch / direct user invisible).

- [ ] **Step 3: Reshape `list_admin_members` in `src/iris/auth/identity.py:214-225`**

Replace with:

```python
    async def list_admin_members(self) -> list[dict[str, str]]:
        """Return everything granted the per-database admin role.

        Each entry is ``{"kind": "user" | "role", "name": <str>}``.
        Includes direct user grantees AND role grantees (e.g. group-roles
        or per-user roles holding the admin tier).
        """
        admin_role = tier_role_name(self.database, TIER_DBADMIN)
        client = self.client

        def _sync() -> list[dict[str, str]]:
            rows = client.query(
                """
                SELECT user_name, role_name FROM system.role_grants
                WHERE granted_role_name = {r:String}
                """,
                {"r": admin_role},
            )
            out: list[dict[str, str]] = []
            for row in rows.named_results():
                u = row.get("user_name")
                r = row.get("role_name")
                if u:
                    out.append({"kind": "user", "name": cast(str, u)})
                elif r:
                    out.append({"kind": "role", "name": cast(str, r)})
            return out

        return await asyncio.to_thread(_sync)
```

The `cast` import is already present at the top of `identity.py:7`.

- [ ] **Step 4: Re-run the tests**

Run: `uv run pytest tests/clickhouse/test_admin_handle.py -v`
Expected: all pass.

- [ ] **Step 5: Verify gates**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/identity.py tests/clickhouse/test_admin_handle.py
git commit -m "fix(identity): list_admin_members returns both user and role grantees"
```

---

## Phase 3 — OIDC hardening

Five tasks, all in `src/iris/auth/providers/oauth.py` and the two OAuth test files. Order matters: do the smaller fixes first, then the structural rewrite.

### Task 11: Replace `assert` in `_verify_id_token` with explicit raise

**Files:**
- Modify: `src/iris/auth/providers/oauth.py:231-247`

- [ ] **Step 1: Patch the assertion**

Replace:

```python
    def _verify_id_token(self, id_token: str) -> None:
        # _verify_id_token is only reached after _request_tokens, which calls
        # self.token_endpoint -> _ensure_discovered() and populates _jwks.
        assert self._jwks is not None, "_jwks must be set before id_token verification"
        try:
```

with:

```python
    def _verify_id_token(self, id_token: str) -> None:
        # _verify_id_token is only reached after _request_tokens, which
        # awaits self._ensure_discovered() and populates _jwks. Guard
        # explicitly: a stripped ``assert`` (python -O) would skip
        # signature verification.
        if self._jwks is None:
            raise AuthError("oauth_exchange")
        try:
```

- [ ] **Step 2: Run OAuth tests**

Run: `uv run pytest tests/auth/test_provider_oauth.py -v`
Expected: existing tests still pass.

- [ ] **Step 3: Verify gates**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`

- [ ] **Step 4: Commit**

```bash
git add src/iris/auth/providers/oauth.py
git commit -m "fix(oauth): explicit raise instead of assert before id_token verification"
```

---

### Task 12: Convert OIDC discovery to lazy async-safe

The current sync `_ensure_discovered` blocks the event loop on first request. Convert to `async`, guard with `asyncio.Lock`, delete the sync `httpx.Client`, and inline endpoint reads at the three call sites.

**Files:**
- Modify: `src/iris/auth/providers/oauth.py` (large diff)
- Modify: `tests/auth/test_provider_oauth.py` (sync property accesses become awaits)

- [ ] **Step 1: Rewrite the constructor and discovery method**

In `src/iris/auth/providers/oauth.py`, replace the entire `__init__` (lines 53-97) and the `_ensure_discovered` + property accessors (lines 99-129) with:

```python
    def __init__(
        self,
        settings: OIDCSettings,
        *,
        _http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        # When _http_transport is set (offline tests), the transport replaces
        # httpx's network stack entirely and `verify` is irrelevant. When it's
        # None, honor settings.ca_cert_path so a private CA can sign the
        # IdP cert.
        verify_arg: bool | ssl.SSLContext = True
        if settings.ca_cert_path:
            verify_arg = ssl.create_default_context(cafile=settings.ca_cert_path)
        if _http_transport is not None:
            # MockTransport implements both sync and async dispatch but only
            # inherits from BaseTransport. Pyright sees BaseTransport and
            # AsyncBaseTransport as unrelated; the double cast through object
            # bypasses that check while preserving the runtime behavior.
            self._async_client = httpx.AsyncClient(
                transport=cast("httpx.AsyncBaseTransport", cast(object, _http_transport)),
                timeout=10.0,
            )
        else:
            self._async_client = httpx.AsyncClient(verify=verify_arg, timeout=10.0)
        # Derive the state-signing key from client_secret so a leak of one
        # is not a leak of the other. The "v1" tag in the prefix lets us
        # rotate the derivation later without invalidating in-flight cookies
        # mid-deploy. SHA-256 is one-way; raw client_secret stays out of
        # the signer.
        derived_key = hashlib.sha256(
            b"iris-oauth-state-signing-v1:" + settings.client_secret.encode()
        ).digest()
        self._signer = URLSafeTimedSerializer(derived_key, salt="iris-oauth-state")
        # Lazy async-safe discovery: first awaiter populates _discovered
        # and _jwks under _discovery_lock; subsequent callers see the
        # cached value. No sync httpx anywhere.
        self._discovery_lock = asyncio.Lock()
        self._discovered: dict[str, Any] | None = None
        self._jwks: jwt.PyJWKSet | None = None

    async def _ensure_discovered(self) -> dict[str, Any]:
        if self._discovered is not None:
            return self._discovered
        async with self._discovery_lock:
            if self._discovered is not None:
                return self._discovered
            discovery_url = (
                self._settings.issuer_url.rstrip("/")
                + "/.well-known/openid-configuration"
            )
            try:
                doc_resp = await self._async_client.get(discovery_url)
                doc_resp.raise_for_status()
                doc = doc_resp.json()
                jwks_resp = await self._async_client.get(doc["jwks_uri"])
                jwks_resp.raise_for_status()
                jwks_doc = jwks_resp.json()
            except Exception as exc:
                logger.exception("auth: OIDC discovery failed")
                raise AuthError("oauth_discovery") from exc
            self._discovered = doc
            self._jwks = jwt.PyJWKSet.from_dict(jwks_doc)
            return doc
```

Also add `import asyncio` at the top of the file (currently absent).

- [ ] **Step 2: Update `close` (sync client gone)**

Replace:

```python
    async def close(self) -> None:
        """Close both httpx clients. Safe to call multiple times."""
        self._client.close()
        await self._async_client.aclose()
```

with:

```python
    async def close(self) -> None:
        """Close the async httpx client. Safe to call multiple times."""
        await self._async_client.aclose()
```

- [ ] **Step 3: Inline endpoint reads at the three call sites**

In `begin` (currently uses `self.authorize_endpoint` via `build_authorize_url`):

```python
    async def begin(self, request: Request) -> Response:
        doc = await self._ensure_discovered()
        redirect_uri = str(request.url_for("login_callback"))
        url, state, verifier = self.build_authorize_url(
            redirect_uri=redirect_uri,
            authorize_endpoint=doc["authorization_endpoint"],
        )
        next_url = request.query_params.get("next", "/")
        signed = self._signer.dumps(
            {"state": state, "verifier": verifier, "next": next_url}
        )
        secure = getattr(request.app.state, "auth_cookie_secure", True)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            signed,
            max_age=STATE_COOKIE_TTL,
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        return response
```

Update `build_authorize_url` to accept the endpoint as an argument (no longer a property):

```python
    def build_authorize_url(
        self, *, redirect_uri: str, authorize_endpoint: str
    ) -> tuple[str, str, str]:
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        params = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self._settings.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{authorize_endpoint}?{urlencode(params)}", state, verifier
```

Update `_request_tokens` to take the token endpoint:

```python
    async def _request_tokens(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> dict[str, Any]:
        doc = await self._ensure_discovered()
        try:
            r = await self._async_client.post(
                doc["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
            )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.exception("auth: OAuth code exchange failed")
            raise AuthError("oauth_exchange") from exc
```

Update `_fetch_userinfo` similarly:

```python
    async def _fetch_userinfo(self, access_token: str) -> dict[str, Any]:
        doc = await self._ensure_discovered()
        try:
            ui = await self._async_client.get(
                doc["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {access_token}"},
            )
            ui.raise_for_status()
            return ui.json()
        except Exception as exc:
            logger.exception("auth: userinfo fetch failed")
            raise AuthError("oauth_exchange") from exc
        # NOTE: changed log message from the previous misleading
        # "OAuth code exchange failed" — userinfo failures are now
        # distinguishable in logs.
```

(Drop the `# NOTE:` comment from the production code; it's just for the plan.)

- [ ] **Step 4: Update existing OAuth tests for async API**

In `tests/auth/test_provider_oauth.py`, the tests that read `provider.authorize_endpoint` etc. as properties need to await discovery instead.

Replace `test_first_property_access_triggers_discovery` (lines 59-64) with:

```python
def test_ensure_discovered_returns_endpoints():
    """The async _ensure_discovered populates the discovery doc with all endpoints."""
    import asyncio
    settings = OIDCSettings(
        issuer_url=ISSUER, client_id="iris", client_secret="shh",
        scopes=("openid", "profile", "email", "groups"),
    )
    provider = OAuthProvider(settings, _http_transport=_signing_mock_transport())
    doc = asyncio.run(provider._ensure_discovered())
    assert doc["authorization_endpoint"] == AUTHZ
    assert doc["token_endpoint"] == TOKEN
    assert doc["userinfo_endpoint"] == USERINFO
```

Replace `test_discovery_failure_surfaces_oauth_discovery_token` (lines 67-77) with:

```python
def test_discovery_failure_surfaces_oauth_discovery_token():
    """If discovery fails, the failure surfaces as AuthError('oauth_discovery')."""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    settings = OIDCSettings(
        issuer_url=ISSUER, client_id="iris", client_secret="shh",
        scopes=("openid",),
    )
    provider = OAuthProvider(settings, _http_transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError) as exc:
        asyncio.run(provider._ensure_discovered())
    assert exc.value.token == "oauth_discovery"
```

Update `test_build_authorize_url_includes_state_and_pkce` (lines 80-88) — `build_authorize_url` now needs the endpoint passed:

```python
def test_build_authorize_url_includes_state_and_pkce(provider):
    url, state, verifier = provider.build_authorize_url(
        redirect_uri="http://localhost/login/callback",
        authorize_endpoint=AUTHZ,
    )
    assert url.startswith(AUTHZ)
    assert "client_id=iris" in url
    assert f"state={state}" in url
    assert "code_challenge=" in url
    assert verifier  # non-empty
```

Add a new test for concurrent first-request safety:

```python
def test_concurrent_ensure_discovered_runs_once():
    """Two coroutines awaiting _ensure_discovered concurrently must trigger
    exactly one network round-trip; subsequent awaits read the cache."""
    import asyncio

    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        # Reuse the signing-mock body for the discovery doc + JWKS only.
        return _signing_mock_transport().handler(request)  # type: ignore[attr-defined]

    settings = OIDCSettings(
        issuer_url=ISSUER, client_id="iris", client_secret="shh",
        scopes=("openid",),
    )
    provider = OAuthProvider(settings, _http_transport=httpx.MockTransport(handler))

    async def _two_callers():
        return await asyncio.gather(
            provider._ensure_discovered(),
            provider._ensure_discovered(),
        )

    asyncio.run(_two_callers())
    discovery_calls = [u for u in fetched if "openid-configuration" in u]
    assert len(discovery_calls) == 1, (
        f"discovery should fire once even with concurrent callers; saw {discovery_calls}"
    )
```

(If the `_signing_mock_transport` fixture returns an `httpx.MockTransport` whose `.handler` attribute isn't accessible, instead define `_two_callers` against a single transport that records calls; the rest of the test logic stands.)

- [ ] **Step 5: Run OAuth unit tests**

Run: `uv run pytest tests/auth/test_provider_oauth.py -v`
Expected: all pass.

- [ ] **Step 6: Run integration test**

Run: `uv run pytest tests/auth/integration/test_oauth_integration.py -v`
Expected: all pass — Keycloak integration exercises the same async paths; no API change visible to it.

- [ ] **Step 7: Verify gates**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`

- [ ] **Step 8: Commit**

```bash
git add src/iris/auth/providers/oauth.py tests/auth/test_provider_oauth.py
git commit -m "refactor(oauth): lazy async-safe OIDC discovery; drop sync httpx.Client"
```

---

### Task 13: id_token canonical sub + nonce + sub-match

Three intertwined changes on the same code path:
- `build_authorize_url` and `begin` add a `nonce` parameter; `nonce` rides in the signed state cookie.
- `_verify_id_token` now returns the decoded claims and verifies the `nonce`.
- `exchange_code` uses the id_token's `sub` and asserts `userinfo["sub"]` matches.

**Files:**
- Modify: `src/iris/auth/providers/oauth.py`
- Modify: `tests/auth/test_provider_oauth.py`

- [ ] **Step 1: Update `build_authorize_url` to accept and emit `nonce`**

```python
    def build_authorize_url(
        self, *, redirect_uri: str, authorize_endpoint: str
    ) -> tuple[str, str, str, str]:
        """Returns (url, state, verifier, nonce)."""
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(64)
        nonce = secrets.token_urlsafe(16)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        params = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self._settings.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "nonce": nonce,
        }
        return f"{authorize_endpoint}?{urlencode(params)}", state, verifier, nonce
```

- [ ] **Step 2: Update `begin` to sign the nonce into the state cookie**

```python
    async def begin(self, request: Request) -> Response:
        doc = await self._ensure_discovered()
        redirect_uri = str(request.url_for("login_callback"))
        url, state, verifier, nonce = self.build_authorize_url(
            redirect_uri=redirect_uri,
            authorize_endpoint=doc["authorization_endpoint"],
        )
        next_url = request.query_params.get("next", "/")
        signed = self._signer.dumps(
            {"state": state, "verifier": verifier, "next": next_url, "nonce": nonce}
        )
        secure = getattr(request.app.state, "auth_cookie_secure", True)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            signed,
            max_age=STATE_COOKIE_TTL,
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        return response
```

- [ ] **Step 3: Update `complete` to thread `nonce` into `exchange_code`**

```python
    async def complete(self, request: Request) -> tuple[User, str]:
        """Returns (user, next_url) on success."""
        signed = request.cookies.get(OAUTH_STATE_COOKIE)
        if not signed:
            raise AuthError("oauth_state")
        try:
            payload = self._signer.loads(signed, max_age=STATE_COOKIE_TTL)
        except BadSignature:
            raise AuthError("oauth_state")
        if request.query_params.get("state") != payload["state"]:
            raise AuthError("oauth_state")
        code = request.query_params.get("code", "")
        if not code:
            raise AuthError("oauth_exchange")
        user = await self.exchange_code(
            code=code,
            code_verifier=payload["verifier"],
            redirect_uri=str(request.url_for("login_callback")),
            expected_nonce=payload["nonce"],
        )
        return user, payload.get("next", "/")
```

- [ ] **Step 4: Reshape `_verify_id_token` to return claims + verify nonce**

```python
    def _verify_id_token(
        self, id_token: str, *, expected_nonce: str
    ) -> dict[str, Any]:
        if self._jwks is None:
            raise AuthError("oauth_exchange")
        try:
            unverified_header = jwt.get_unverified_header(id_token)
            signing_key = self._jwks[unverified_header["kid"]].key
            claims = jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256", "ES256"],
                audience=self._settings.client_id,
                issuer=self._settings.issuer_url.rstrip("/"),
                options={"require": ["sub", "iat", "exp", "aud", "iss", "nonce"]},
            )
        except (jwt.InvalidTokenError, KeyError) as exc:
            logger.exception("auth: id_token verification failed")
            raise AuthError("oauth_exchange") from exc
        if claims.get("nonce") != expected_nonce:
            logger.warning("auth: id_token nonce mismatch")
            raise AuthError("oauth_exchange")
        return claims
```

- [ ] **Step 5: Reshape `exchange_code` to use id_token sub + assert match**

```python
    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str, expected_nonce: str
    ) -> User:
        token_response = await self._request_tokens(
            code=code, code_verifier=code_verifier, redirect_uri=redirect_uri
        )
        id_token = token_response.get("id_token")
        if not id_token:
            logger.error("auth: token endpoint returned no id_token")
            raise AuthError("oauth_exchange")
        id_claims = self._verify_id_token(id_token, expected_nonce=expected_nonce)
        try:
            access_token = token_response["access_token"]
        except KeyError as exc:
            logger.exception("auth: token response missing access_token")
            raise AuthError("oauth_exchange") from exc
        ui_claims = await self._fetch_userinfo(access_token)
        if ui_claims.get("sub") != id_claims["sub"]:
            logger.warning(
                "auth: userinfo.sub does not match id_token.sub (potential token substitution)"
            )
            raise AuthError("oauth_sub_mismatch")
        return self._user_from_id_and_userinfo(
            id_claims=id_claims, ui_claims=ui_claims
        )
```

- [ ] **Step 6: Update `_user_from_claims` → `_user_from_id_and_userinfo`**

Replace the existing `_user_from_claims`:

```python
    def _user_from_id_and_userinfo(
        self, *, id_claims: dict[str, Any], ui_claims: dict[str, Any]
    ) -> User:
        sub = str(id_claims["sub"])  # safe: jwt.decode required 'sub'
        raw_groups = ui_claims.get("groups", [])
        if not isinstance(raw_groups, list):
            logger.warning(
                "auth: OIDC userinfo `groups` is not a list (got %s); ignoring",
                type(raw_groups).__name__,
            )
            raw_groups = []
        groups = tuple(str(g) for g in raw_groups)
        if not groups:
            logger.warning(
                "auth: OIDC userinfo had no `groups` claim — check IdP client mapper"
            )
        username = str(ui_claims.get("preferred_username") or sub)
        return User(
            subject=sub,
            username=username,
            display_name=str(ui_claims.get("name") or username),
            groups=groups,
        )
```

- [ ] **Step 7: Update existing test fixtures for the new contract**

Tests need: (a) the mock IdP to embed the request `nonce` into the id_token; (b) the mock IdP to put the same `sub` in id_token and userinfo; (c) callers of `exchange_code` to pass `expected_nonce`; (d) callers of `build_authorize_url` to expect a 4-tuple.

In `tests/auth/test_provider_oauth.py`, find `_signing_mock_transport` (or similar helper used by `provider` fixture) and update it to:
- Capture the `nonce` passed in the authorize request (or accept a known nonce from the test).
- Embed it in the id_token alongside `sub`, `iat`, `exp`, `aud`, `iss`.
- Issue userinfo with the same `sub`.

Concrete change in the helper (whose body is below the imports — find where the id_token is built and add the nonce claim):

```python
# In _signing_mock_transport, where the token-endpoint response is built:
def _build_id_token(*, sub: str, nonce: str, key, kid: str) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": sub,
        "aud": "iris",
        "iat": now,
        "exp": now + 3600,
        "nonce": nonce,
    }
    return pyjwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})
```

Update each test that calls `provider.exchange_code(...)` to pass `expected_nonce="<value>"` matching whatever the mock embeds. If the mock currently uses a constant nonce, pass that constant.

Update `test_complete_callback_returns_user` (line 91 onward) to write the cookie shape `complete` now expects (`payload["nonce"]` present).

- [ ] **Step 8: Add new tests for nonce mismatch, sub mismatch, missing sub, malformed groups**

Append to `tests/auth/test_provider_oauth.py`:

```python
def test_nonce_mismatch_is_rejected():
    """If the id_token's nonce doesn't match the cookie's nonce, fail."""
    import asyncio

    settings = OIDCSettings(
        issuer_url=ISSUER, client_id="iris", client_secret="shh",
        scopes=("openid",),
    )
    # Mock issues an id_token with a fixed nonce; we pass a different one.
    provider = OAuthProvider(
        settings, _http_transport=_signing_mock_transport(token_nonce="alpha")
    )
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="dummy",
                code_verifier="v",
                redirect_uri="http://localhost/cb",
                expected_nonce="beta",
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_sub_mismatch_is_rejected():
    """If userinfo.sub != id_token.sub, fail with oauth_sub_mismatch."""
    import asyncio

    settings = OIDCSettings(
        issuer_url=ISSUER, client_id="iris", client_secret="shh",
        scopes=("openid",),
    )
    provider = OAuthProvider(
        settings,
        _http_transport=_signing_mock_transport(
            id_sub="alice", userinfo_sub="bob"
        ),
    )
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="dummy",
                code_verifier="v",
                redirect_uri="http://localhost/cb",
                expected_nonce="n",
            )
        )
    assert exc.value.token == "oauth_sub_mismatch"


def test_groups_not_a_list_is_treated_as_empty(caplog):
    """If userinfo returns groups as a string instead of a list, ignore it
    and log a warning (do not iterate per-character)."""
    import asyncio

    settings = OIDCSettings(
        issuer_url=ISSUER, client_id="iris", client_secret="shh",
        scopes=("openid",),
    )
    provider = OAuthProvider(
        settings,
        _http_transport=_signing_mock_transport(userinfo_groups="admin"),  # string
    )
    user = asyncio.run(
        provider.exchange_code(
            code="dummy",
            code_verifier="v",
            redirect_uri="http://localhost/cb",
            expected_nonce="n",
        )
    )
    assert user.groups == ()
    assert any("not a list" in rec.message for rec in caplog.records)
```

These new tests assume `_signing_mock_transport` accepts the parameters `token_nonce`, `id_sub`, `userinfo_sub`, `userinfo_groups`. If the helper doesn't yet take them, extend its signature accordingly when you update it in Step 7.

- [ ] **Step 9: Run OAuth unit tests**

Run: `uv run pytest tests/auth/test_provider_oauth.py -v`
Expected: all pass.

- [ ] **Step 10: Run Keycloak integration test**

Run: `uv run pytest tests/auth/integration/test_oauth_integration.py -v`
Expected: pass. Keycloak supports `nonce` natively when sent on `/auth`; the response carries it through into the id_token. No Keycloak-side config change required.

- [ ] **Step 11: Verify gates**

Run: `uv run ruff check && uv run basedpyright --level error && uv run pytest -x`

- [ ] **Step 12: Commit**

```bash
git add src/iris/auth/providers/oauth.py tests/auth/test_provider_oauth.py
git commit -m "fix(oauth): id_token canonical sub + nonce + userinfo.sub assertion"
```

---

## Phase 4 — Service-locator typing

One task, mostly mechanical. No behavior change.

### Task 14: Type `AuthSession` refs as `Optional[concrete]` + `_ch()` helper

**Files:**
- Modify: `src/iris/auth/identity.py`
- Modify: `src/iris/auth/deps.py`
- Possibly: `tests/auth/conftest.py`, `tests/clickhouse/conftest.py` (if either constructs `AuthSession` literals with untyped refs)

- [ ] **Step 1: Add the imports + type the fields in `AuthSession`**

In `src/iris/auth/identity.py`, add at the top (after the `from __future__ import annotations` line):

```python
from typing import TYPE_CHECKING

import httpx
from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings

if TYPE_CHECKING:
    # Avoid the import cycle: sessions.py imports User/UserSession from
    # identity.py at runtime, so identity.py can only reference SessionStore
    # in type-checker mode. With ``from __future__ import annotations``
    # already in effect, the ``store: SessionStore`` annotation is a
    # string at runtime — no resolution needed.
    from iris.auth.sessions import SessionStore
```

(The existing `from clickhouse_connect.driver.query import QueryResult` stays; this just adds `Client`.)

Replace the four `Any`-typed fields in `AuthSession` (currently lines 88-91):

```python
    client: Client | None = field(repr=False, compare=False)
    http_client: httpx.AsyncClient | None = field(repr=False, compare=False)
    settings: ClickHouseSettings | None = field(repr=False, compare=False)
    store: SessionStore = field(repr=False, compare=False)
```

- [ ] **Step 2: Add the `_ch()` helper on `AuthSession`**

Below `persist_data` (after line 100):

```python
    def _ch(self) -> tuple[Client, httpx.AsyncClient, ClickHouseSettings]:
        """Return the CH refs as a non-None triple, or raise if CH isn't installed.

        Subclasses that perform CH operations call this once at the top of
        each method instead of reading ``self.client`` etc. directly. The
        Optional fields exist to support ``build_app(install_clickhouse=False)``
        (used by the auth-only test layer); routes that need CH are
        guarded by the alias deps that already require CH-capable
        ``Rights``, so by the time a method on a CH-using subclass runs
        the refs are populated.
        """
        if self.client is None or self.http_client is None or self.settings is None:
            raise RuntimeError(
                "ClickHouse not installed; this method requires "
                "build_app(install_clickhouse=True)"
            )
        return self.client, self.http_client, self.settings
```

- [ ] **Step 3: Update CH-using methods to use `_ch()`**

For every method on `DatabaseSession`, `DatabaseAdminSession`, `DatabaseCreatorSession`, `AdminSession` that currently reads `self.client` / `self.http_client` / `self.settings`, replace the ad-hoc reads with one `_ch()` call at the top.

Example for `DatabaseSession.query_as_user` (currently lines 112-123):

```python
    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        _client, http_client, _settings = self._ch()
        return await query_as_user(
            http_client,
            username=self.user.username,
            sql=sql,
            parameters=parameters,
            database=self.database,
        )
```

Apply the same pattern to:
- All 12 grant/revoke methods in `DatabaseAdminSession` (each reads `self.client`).
- `DatabaseAdminSession.delete_database`.
- `DatabaseAdminSession.list_admin_members`, `list_grants`, `list_row_policies`.
- `DatabaseCreatorSession.create_database`.
- All `AdminSession` methods that touch `self.client` or `self.settings`.

The `query_as_service` on `AdminSession` does not need `_settings`, only `client`.

- [ ] **Step 4: Drop now-unused `Any` imports if applicable**

In `identity.py`, `from typing import Any, cast` stays (because `data: dict[str, Any]` and `cast(str, ...)` are still in use).

In `deps.py`, the resolvers still pass refs by name — no change beyond the fact that they now flow `Client | None` etc. through. Keep `Any` imports if any remain in actual annotations; otherwise, remove. Run `ruff` to find unused imports if uncertain.

- [ ] **Step 5: Run the full type and test gates**

Run: `uv run basedpyright --level error`
Expected: zero errors. **If pyright surfaces previously-hidden type bugs**, fix them inline (these are the bugs the typing tightening exposes).

Run: `uv run basedpyright --level warning`
Expected: zero warnings.

Run: `uv run ruff check && uv run pytest -x`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/identity.py src/iris/auth/deps.py
# Add any test files that needed typed-ref updates:
# git add tests/auth/conftest.py tests/clickhouse/conftest.py
git commit -m "refactor(auth): type Session CH refs as Optional[concrete] + _ch() helper"
```

---

## Final verification

After all 14 tasks land:

- [ ] **Run the entire test suite once more from clean.**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
uv run pytest
```

Expected: all clean.

- [ ] **Skim `git log --oneline main..HEAD`** — should be 14 commits, each with a descriptive subject. No `WIP:` or `fixup!` slipped through.

- [ ] **Check for any lingering references to the deleted constructs:**

```bash
grep -rn "iris.clickhouse.handle\|CLICKHOUSE_SERVICE_ADMIN_USER\|httpx.Client\b" src/ tests/
```

The only `httpx.Client` matches should be inside test mocks (`httpx.MockTransport(...)` is fine; `httpx.Client(...)` should be gone from `src/`).

---

## Self-review notes

This plan was checked against the spec section by section. Coverage:

| Spec item | Task |
|---|---|
| 6.1 stale references | Task 2 |
| 6.2 `__future__` imports | Task 1 |
| 6.3 prune docstrings | Task 3 |
| 2.2 dead branch | Task 4 |
| 1.9 405 Allow header | Task 5 |
| 1.4 revoke role leak | Task 6 |
| 1.6 BEGIN IMMEDIATE | Task 7 |
| 1.7 CSRF cookie sanity | Task 8 |
| 3.3 param marshaler | Task 9 |
| 2.1 list_admin_members | Task 10 |
| 1.2 assert→raise | Task 11 |
| 3.1 lazy async-safe | Task 12 |
| 1.1 sub + 1.3 claim validation | Task 13 |
| 3.2 typed refs | Task 14 |

13 spec items; 14 tasks (Task 1 covers `__future__` for 5 files in one commit; Task 13 bundles 1.1 + 1.3 because they touch the same code path).

No placeholders. No "TODO" / "TBD". Method/property name consistency: `_ensure_discovered`, `build_authorize_url(redirect_uri, authorize_endpoint)`, `_user_from_id_and_userinfo`, `_ch()`, `_marshal_param` are used consistently across the tasks where they appear.
