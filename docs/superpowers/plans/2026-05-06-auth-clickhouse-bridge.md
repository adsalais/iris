# Auth ↔ ClickHouse Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the existing `iris.auth` and `iris.clickhouse` packages so logged-in users can issue impersonated ClickHouse queries through a typed FastAPI handle, and `init_user_rights` runs once per real authentication.

**Architecture:** Two handle classes (`ClickHouseHandle`, `ClickHouseAdminHandle`) wrap a shared app-scoped `clickhouse-connect` client. Two FastAPI deps (`get_clickhouse_handle`, `require_clickhouse_admin`) inject the right handle for a route. A generic post-login hook list on `app.state` lets `iris.clickhouse` register provisioning without `iris.auth` knowing about it.

**Tech Stack:** FastAPI, `clickhouse-connect`, basedpyright, pytest, `testcontainers-python`.

**Spec:** `docs/superpowers/specs/2026-05-06-auth-clickhouse-bridge-design.md`.

---

## File Structure

NEW files:

| Path | Responsibility |
|---|---|
| `src/iris/clickhouse/handle.py` | `ClickHouseHandle`, `ClickHouseAdminHandle` — plain-data classes, no auth import |
| `src/iris/clickhouse/deps.py` | `get_clickhouse_handle`, `require_clickhouse_admin`, `CLICKHOUSE_ADMIN_ROLE` |
| `src/iris/clickhouse/install.py` | `install(app)` — builds Client, runs `ensure_service_admin`, registers post-login hook |
| `tests/clickhouse/test_handle.py` | Unit tests for handles against a mocked `Client` |
| `tests/clickhouse/test_handle_integration.py` | Integration tests for `EXECUTE AS` against the CH testcontainer |
| `tests/clickhouse/test_clickhouse_deps.py` | Unit tests for the two FastAPI deps |
| `tests/clickhouse/test_login_provisioning.py` | End-to-end: form-login through `TestClient` provisions user in CH |

MODIFIED files:

| Path | Change |
|---|---|
| `src/iris/auth/routes.py` | `_finalize_login_redirect` iterates `app.state.post_login_hooks`; `install` initializes the list to `[]` |
| `src/iris/clickhouse/__init__.py` | Re-export the new public surface |
| `src/iris/app.py` | `build_app(*, install_clickhouse: bool = True)`; calls `iris.clickhouse.install(app)` after `iris.auth.install(app)` when flag is true |
| `tests/conftest.py` | `app` fixture passes `install_clickhouse=False`; YAML fixture adds `clickhouse_admin` role mapped to the `admins` group |
| `CLAUDE.md` | Document the new public surface and the post-login hook seam |

---

## Task 1: Auth post-login hook seam

**Files:**
- Modify: `src/iris/auth/routes.py:62-82`, `src/iris/auth/routes.py:170-200` (install function)
- Test: `tests/auth/test_post_login_hook.py` (NEW)

The auth layer grows a generic ordered list of async callables that fire once per real authentication (form-login submit success or OAuth callback success). Auth never references ClickHouse — it just iterates whatever's registered.

- [ ] **Step 1.1: Write the failing test**

Create `tests/auth/test_post_login_hook.py`:

```python
"""The auth layer exposes a generic post-login hook list. iris.clickhouse and any
future bridge can append to it without auth depending on them."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from iris.app import build_app
from iris.auth.identity import User


def _login(client: TestClient) -> None:
    response = client.post(
        "/login",
        data={"username": "alice", "password": "secret", "next": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text


def test_post_login_hook_fires_on_form_login() -> None:
    app = build_app(install_clickhouse=False)
    seen: list[User] = []

    async def hook(user: User) -> None:
        seen.append(user)

    app.state.post_login_hooks.append(hook)

    # Need to pre-issue a CSRF token before POST /login.
    client = TestClient(app)
    client.get("/login")  # mints CSRF cookie
    csrf = client.cookies.get("iris_csrf")
    assert csrf is not None
    response = client.post(
        "/login",
        data={"username": "alice", "password": "secret", "next": "/", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text

    assert len(seen) == 1
    assert seen[0].username == "alice"
    assert "admins" in seen[0].groups


def test_post_login_hook_exception_is_fail_loud() -> None:
    app = build_app(install_clickhouse=False)

    async def hook(_user: User) -> None:
        raise RuntimeError("boom")

    app.state.post_login_hooks.append(hook)

    client = TestClient(app, raise_server_exceptions=False)
    client.get("/login")
    csrf = client.cookies.get("iris_csrf")
    assert csrf is not None
    response = client.post(
        "/login",
        data={"username": "alice", "password": "secret", "next": "/", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.status_code == 500


def test_post_login_hooks_default_to_empty_list_after_install() -> None:
    app = build_app(install_clickhouse=False)
    assert isinstance(app.state.post_login_hooks, list)
    assert app.state.post_login_hooks == []
```

- [ ] **Step 1.2: Run the test to verify it fails**

```
uv run pytest tests/auth/test_post_login_hook.py -v
```
Expected: FAIL — `build_app()` doesn't accept `install_clickhouse`, `app.state.post_login_hooks` doesn't exist.

- [ ] **Step 1.3: Add the seam to `iris.auth.routes`**

In `src/iris/auth/routes.py`, modify `_finalize_login_redirect` (around line 62) to iterate hooks before logging the success line:

```python
async def _finalize_login_redirect(
    *, user: User, target: str, method: str
) -> RedirectResponse:
    session = await store.create(user)
    for hook in getattr(app.state, "post_login_hooks", ()):
        await hook(user)
    logger.info(
        "auth: login user=%s subject=%s method=%s groups=%s",
        ...
    )
```

Note: `app` is not in scope inside `_finalize_login_redirect` as written today — it's a closure inside `build_auth_router`. Two ways to plumb it in:

(a) Add a parameter `app: FastAPI` to `build_auth_router` and capture in the closure.
(b) Use `request.app.state` (requires the finalize function to take a `Request`).

Choose (a) — minimal change. Update `build_auth_router` signature:

```python
def build_auth_router(
    *,
    app: FastAPI,           # NEW
    provider: Provider,
    store: InMemorySessionStore,
    cookie_name: str,
    cookie_secure: bool,
    ttl_seconds: int,
) -> APIRouter:
    router = APIRouter()
    login_bucket = TokenBucket(capacity=10, refill_per_second=0.2)

    async def _finalize_login_redirect(
        *, user: User, target: str, method: str
    ) -> RedirectResponse:
        session = await store.create(user)
        for hook in app.state.post_login_hooks:
            await hook(user)
        logger.info(...)
        ...
```

In `install` (around line 173), pass `app` and initialize the hook list:

```python
def install(app: FastAPI) -> None:
    settings = AuthSettings.from_env()
    ...
    app.state.post_login_hooks = []          # NEW — must precede router build
    router = build_auth_router(
        app=app,
        provider=provider,
        store=store,
        cookie_name=settings.cookie_name,
        cookie_secure=settings.cookie_secure,
        ttl_seconds=settings.ttl_seconds,
    )
    ...
```

- [ ] **Step 1.4: Add `install_clickhouse` parameter to `build_app`**

In `src/iris/app.py`, change:

```python
def build_app() -> FastAPI:
```

to:

```python
def build_app(*, install_clickhouse: bool = True) -> FastAPI:
```

The body keeps the existing `install_auth(app)` call. The `install_clickhouse` branch is added in Task 6; for now, just accept and ignore the parameter.

- [ ] **Step 1.5: Run the test to verify it passes**

```
uv run pytest tests/auth/test_post_login_hook.py -v
```
Expected: PASS for all three tests.

- [ ] **Step 1.6: Run the full auth suite to confirm no regressions**

```
uv run pytest tests/auth -x
```
Expected: PASS.

- [ ] **Step 1.7: Type-check**

```
uv run basedpyright --level error
```
Expected: no errors.

- [ ] **Step 1.8: Commit**

```
git add src/iris/auth/routes.py src/iris/app.py tests/auth/test_post_login_hook.py
git commit -m "feat(auth): generic post-login hook seam

_finalize_login_redirect iterates app.state.post_login_hooks. Hooks
receive the User and can raise to fail-loud the login. iris.clickhouse
will register provisioning here without auth depending on it."
```

---

## Task 2: ClickHouseHandle (user-impersonated queries)

**Files:**
- Create: `src/iris/clickhouse/handle.py`
- Test: `tests/clickhouse/test_handle.py`

The user handle wraps the shared `Client` and exposes a single async method that prepends `EXECUTE AS <quoted_username>` to every SQL statement. Username is validated and quoted via the existing `quote_identifier` helper.

The exact `EXECUTE AS` syntax (per the user-provided correction in the spec):

```sql
EXECUTE AS target_user SELECT * FROM some_table
```

Single statement, no internal semicolon. The handle constructs `f"EXECUTE AS {quoted_username} {sql}"`.

- [ ] **Step 2.1: Write the failing test**

Create `tests/clickhouse/test_handle.py`:

```python
"""Unit tests for ClickHouseHandle against a mocked Client."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from clickhouse_connect.driver.query import QueryResult

from iris.clickhouse.handle import ClickHouseHandle


@pytest.mark.asyncio
async def test_query_as_user_prepends_execute_as() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)

    handle = ClickHouseHandle(client=client, username="alice")
    await handle.query_as_user("SELECT 1")

    args, kwargs = client.query.call_args
    sql = args[0] if args else kwargs["query"]
    assert sql.startswith("EXECUTE AS `alice` "), sql
    assert sql.endswith("SELECT 1"), sql


@pytest.mark.asyncio
async def test_query_as_user_passes_parameters() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)

    handle = ClickHouseHandle(client=client, username="alice")
    await handle.query_as_user("SELECT {x:Int32}", parameters={"x": 7})

    _args, kwargs = client.query.call_args
    assert kwargs["parameters"] == {"x": 7}


@pytest.mark.asyncio
async def test_handle_rejects_invalid_username() -> None:
    client = MagicMock()
    with pytest.raises(ValueError):
        ClickHouseHandle(client=client, username="alice; DROP USER bob")
```

- [ ] **Step 2.2: Run the test to verify it fails**

```
uv run pytest tests/clickhouse/test_handle.py -v
```
Expected: FAIL — `iris.clickhouse.handle` doesn't exist.

- [ ] **Step 2.3: Implement `ClickHouseHandle`**

Create `src/iris/clickhouse/handle.py`:

```python
"""Per-request ClickHouse handle classes used by FastAPI route handlers.

The handle wraps the app-scoped Client and a username; it doesn't open or close
connections. Each method wraps the sync clickhouse-connect call in
``asyncio.to_thread`` so a slow query doesn't block the FastAPI event loop.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.query import QueryResult

from iris.clickhouse.identifiers import quote_identifier


class ClickHouseHandle:
    """Per-request handle for any logged-in user.

    Exposes only ``query_as_user``, which prepends ``EXECUTE AS <quoted_username>``
    to the SQL so the query runs under the user's CH identity. Service-identity
    queries and admin functions are not exposed here — see ``ClickHouseAdminHandle``.
    """

    def __init__(self, *, client: Client, username: str) -> None:
        self._client = client
        self._username_quoted = quote_identifier(username, kind="username")
        self._username = username

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        impersonated = f"EXECUTE AS {self._username_quoted} {sql}"
        return await asyncio.to_thread(
            self._client.query,
            impersonated,
            parameters=dict(parameters) if parameters else None,
        )
```

- [ ] **Step 2.4: Run the test to verify it passes**

```
uv run pytest tests/clickhouse/test_handle.py -v
```
Expected: PASS.

- [ ] **Step 2.5: Type-check**

```
uv run basedpyright --level error
```
Expected: no errors.

- [ ] **Step 2.6: Commit**

```
git add src/iris/clickhouse/handle.py tests/clickhouse/test_handle.py
git commit -m "feat(clickhouse): ClickHouseHandle for user-impersonated queries

Per-request handle that prepends EXECUTE AS <user> to every query so a
shared app-scoped client can serve concurrent requests safely. Username
is validated and quoted via quote_identifier."
```

---

## Task 3: ClickHouseAdminHandle (service queries + admin functions)

**Files:**
- Modify: `src/iris/clickhouse/handle.py`
- Test: `tests/clickhouse/test_handle.py` (extend)

The admin handle adds service-identity queries (no `EXECUTE AS`) and async wrappers around the existing module-level admin/audit functions (`reprovision_user`, `grant_*`, `add_row_policy`, `revoke_row_policy`, `user_grants`, etc.).

- [ ] **Step 3.1: Write the failing tests**

Append to `tests/clickhouse/test_handle.py`:

```python
from unittest.mock import patch

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.handle import ClickHouseAdminHandle


def _admin_handle(client: Any) -> ClickHouseAdminHandle:
    settings = ClickHouseSettings(
        host="h", port=1, user="u", password="p", secure=True, verify=True,
        ca_cert_path=None,
        service_admin_user="iris_svc",
        service_admin_role="service_admin_role",
    )
    return ClickHouseAdminHandle(client=client, username="alice", settings=settings)


@pytest.mark.asyncio
async def test_admin_handle_subclasses_user_handle() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)
    handle = _admin_handle(client)

    assert isinstance(handle, ClickHouseHandle)
    await handle.query_as_user("SELECT 1")
    args, _kwargs = client.query.call_args
    assert args[0].startswith("EXECUTE AS `alice` ")


@pytest.mark.asyncio
async def test_query_as_service_does_not_prepend_execute_as() -> None:
    client = MagicMock()
    client.query.return_value = MagicMock(spec=QueryResult)
    handle = _admin_handle(client)

    await handle.query_as_service("SELECT 1")
    args, _kwargs = client.query.call_args
    sql = args[0] if args else _kwargs["query"]
    assert "EXECUTE AS" not in sql
    assert sql == "SELECT 1"


@pytest.mark.asyncio
async def test_reprovision_user_delegates_to_init_user_rights() -> None:
    client = MagicMock()
    handle = _admin_handle(client)

    with patch("iris.clickhouse.handle.init_user_rights") as mock_init:
        await handle.reprovision_user(username="bob", groups=["sales"])

    mock_init.assert_called_once()
    _, kwargs = mock_init.call_args
    assert kwargs["username"] == "bob"
    assert kwargs["groups"] == ["sales"]


@pytest.mark.asyncio
async def test_admin_audit_methods_delegate() -> None:
    client = MagicMock()
    handle = _admin_handle(client)

    with patch("iris.clickhouse.handle.user_grants") as mock_ug:
        mock_ug.return_value = [{"x": 1}]
        result = await handle.user_grants(username="alice")
    assert result == [{"x": 1}]
    mock_ug.assert_called_once()
```

- [ ] **Step 3.2: Run the tests to verify they fail**

```
uv run pytest tests/clickhouse/test_handle.py -v
```
Expected: FAIL — `ClickHouseAdminHandle` doesn't exist.

- [ ] **Step 3.3: Implement `ClickHouseAdminHandle`**

Append to `src/iris/clickhouse/handle.py`:

```python
from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.grants import (
    grant_insert_update_to_table,
    grant_select_to_database,
)
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.users import init_user_rights


class ClickHouseAdminHandle(ClickHouseHandle):
    """Admin-capable handle for routes gated on the ``clickhouse_admin`` role.

    Adds service-identity queries (no impersonation) and async wrappers around
    the existing module-level admin/audit functions. ``query_as_user`` is
    inherited from the parent class.
    """

    def __init__(
        self,
        *,
        client: Client,
        username: str,
        settings: ClickHouseSettings,
    ) -> None:
        super().__init__(client=client, username=username)
        self._settings = settings

    async def query_as_service(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> QueryResult:
        return await asyncio.to_thread(
            self._client.query,
            sql,
            parameters=dict(parameters) if parameters else None,
        )

    async def reprovision_user(self, *, username: str, groups: list[str]) -> None:
        await asyncio.to_thread(
            init_user_rights,
            self._client,
            username=username,
            groups=groups,
            settings=self._settings,
        )

    async def grant_select_to_database(self, *, database: str, role: str) -> None:
        await asyncio.to_thread(
            grant_select_to_database,
            self._client,
            database=database,
            role=role,
        )

    async def grant_insert_update_to_table(
        self, *, database: str, table: str, role: str
    ) -> None:
        await asyncio.to_thread(
            grant_insert_update_to_table,
            self._client,
            database=database,
            table=table,
            role=role,
        )

    async def add_row_policy(
        self,
        *,
        database: str,
        table: str,
        column: str,
        role: str,
        value: str,
    ) -> None:
        await asyncio.to_thread(
            add_row_policy,
            self._client,
            database=database,
            table=table,
            column=column,
            role=role,
            value=value,
            settings=self._settings,
        )

    async def revoke_row_policy(
        self,
        *,
        database: str,
        table: str,
        role: str,
        value: str,
    ) -> None:
        await asyncio.to_thread(
            revoke_row_policy,
            self._client,
            database=database,
            table=table,
            role=role,
            value=value,
        )

    async def user_grants(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(user_grants, self._client, username=username)

    async def role_grants(self, *, role: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(role_grants, self._client, role=role)

    async def user_role_memberships(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            user_role_memberships, self._client, username=username
        )

    async def user_row_policies(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            user_row_policies, self._client, username=username
        )

    async def role_row_policies(self, *, role: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            role_row_policies, self._client, role=role
        )

    async def table_row_policies(
        self, *, database: str, table: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            table_row_policies, self._client, database=database, table=table
        )
```

- [ ] **Step 3.4: Run the tests to verify they pass**

```
uv run pytest tests/clickhouse/test_handle.py -v
```
Expected: PASS.

- [ ] **Step 3.5: Type-check**

```
uv run basedpyright --level error
```
Expected: no errors.

- [ ] **Step 3.6: Commit**

```
git add src/iris/clickhouse/handle.py tests/clickhouse/test_handle.py
git commit -m "feat(clickhouse): ClickHouseAdminHandle adds service queries + admin functions

Subclasses ClickHouseHandle so admin routes inherit query_as_user and add
query_as_service (no EXECUTE AS) plus async wrappers around init_user_rights,
the grant_* / row-policy helpers, and the audit functions."
```

---

## Task 4: `get_clickhouse_handle` dep (any logged-in user)

**Files:**
- Create: `src/iris/clickhouse/deps.py`
- Test: `tests/clickhouse/test_clickhouse_deps.py`

The dep takes a `Request` and a `Session`, reads the shared client off `request.app.state.clickhouse_client`, and returns a `ClickHouseHandle` bound to the session's username.

- [ ] **Step 4.1: Write the failing test**

Create `tests/clickhouse/test_clickhouse_deps.py`:

```python
"""Unit tests for the ClickHouse FastAPI deps."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.identity import User
from iris.auth.session import Session
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.deps import (
    CLICKHOUSE_ADMIN_ROLE,
    get_clickhouse_handle,
)
from iris.clickhouse.handle import ClickHouseHandle


def _settings() -> ClickHouseSettings:
    return ClickHouseSettings(
        host="h", port=1, user="u", password="p", secure=True, verify=True,
        ca_cert_path=None,
        service_admin_user="iris_svc",
        service_admin_role="service_admin_role",
    )


def _session(*, roles: frozenset[str] = frozenset()) -> Session:
    user = User(
        subject="mock:alice",
        username="alice",
        display_name="Alice",
        groups=("admins",),
    )
    now = datetime.now(UTC)
    return Session(
        id="sid",
        user=user,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        roles=roles,
    )


def _make_app() -> tuple[FastAPI, MagicMock]:
    app = FastAPI()
    client = MagicMock()
    app.state.clickhouse_client = client
    app.state.clickhouse_settings = _settings()

    from iris.auth.deps import Session as AuthSession  # alias

    @app.get("/use")
    async def use(handle: ClickHouseHandle = Depends(get_clickhouse_handle)) -> dict[str, Any]:
        return {"username": handle._username}

    return app, client


def test_get_clickhouse_handle_returns_handle_for_session(monkeypatch) -> None:
    """The dep injects a ClickHouseHandle bound to the session's username.

    We bypass the real auth dep by overriding it on the app — the focus
    of this test is the CH dep, not the auth chain.
    """
    from iris.auth.deps import Session as AuthSession

    app, _client = _make_app()

    async def fake_session() -> Session:
        return _session()

    # Replace the auth dep on this app instance only.
    from iris.auth.deps import _build_required
    app.dependency_overrides[_build_required] = fake_session

    response = TestClient(app).get("/use")
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}
```

- [ ] **Step 4.2: Run the test to verify it fails**

```
uv run pytest tests/clickhouse/test_clickhouse_deps.py -v
```
Expected: FAIL — `iris.clickhouse.deps` doesn't exist.

- [ ] **Step 4.3: Implement `get_clickhouse_handle`**

Create `src/iris/clickhouse/deps.py`:

```python
"""FastAPI dependencies that bridge iris.auth into iris.clickhouse.

These deps are the only place in iris.clickhouse that imports from iris.auth.
The handle classes in handle.py and the rest of the package stay independent
of auth.
"""
from __future__ import annotations

from typing import Final

from fastapi import Request

from iris.auth.deps import Session
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.authz.core import CurrentMapping
from iris.clickhouse.handle import ClickHouseAdminHandle, ClickHouseHandle

CLICKHOUSE_ADMIN_ROLE: Final = "clickhouse_admin"


async def get_clickhouse_handle(
    request: Request, session: Session
) -> ClickHouseHandle:
    """Return a user-handle bound to the session's username. Any logged-in user."""
    return ClickHouseHandle(
        client=request.app.state.clickhouse_client,
        username=session.user.username,
    )
```

(`require_clickhouse_admin` is added in Task 5.)

- [ ] **Step 4.4: Run the test to verify it passes**

```
uv run pytest tests/clickhouse/test_clickhouse_deps.py -v
```
Expected: PASS.

- [ ] **Step 4.5: Type-check**

```
uv run basedpyright --level error
```
Expected: no errors.

- [ ] **Step 4.6: Commit**

```
git add src/iris/clickhouse/deps.py tests/clickhouse/test_clickhouse_deps.py
git commit -m "feat(clickhouse): get_clickhouse_handle dep for logged-in users

Bridges iris.auth.Session into iris.clickhouse.ClickHouseHandle. The dep
reads the shared app-scoped client off app.state and returns a handle
bound to the session's username."
```

---

## Task 5: `require_clickhouse_admin` dep (role-gated)

**Files:**
- Modify: `src/iris/clickhouse/deps.py`
- Test: `tests/clickhouse/test_clickhouse_deps.py` (extend)

The role-gate dep mirrors `iris.auth.authz.deps.require_role`:
- 500 (`AuthorizationMisconfigured`) if `clickhouse_admin` is not defined in the YAML.
- 403 (`AuthForbidden`) if the user lacks the role.
- Returns `ClickHouseAdminHandle` on success.

- [ ] **Step 5.1: Write the failing tests**

Append to `tests/clickhouse/test_clickhouse_deps.py`:

```python
from iris.auth.authz.mapping import RoleDef, RoleMapping
from iris.clickhouse.deps import require_clickhouse_admin
from iris.clickhouse.handle import ClickHouseAdminHandle


def _mapping_with_admin_role() -> RoleMapping:
    role = RoleDef(
        name=CLICKHOUSE_ADMIN_ROLE,
        groups=frozenset({"admins"}),
        users_lower=frozenset(),
        includes=(),
    )
    return RoleMapping(
        roles={CLICKHOUSE_ADMIN_ROLE: role},
        closure={CLICKHOUSE_ADMIN_ROLE: frozenset({CLICKHOUSE_ADMIN_ROLE})},
    )


def _mapping_without_admin_role() -> RoleMapping:
    return RoleMapping(roles={}, closure={})


def _admin_app(mapping: RoleMapping, session_roles: frozenset[str]):
    app, client = _make_app()

    async def fake_session() -> Session:
        return _session(roles=session_roles)

    async def fake_mapping() -> RoleMapping:
        return mapping

    from iris.auth.authz.core import current_mapping
    from iris.auth.deps import _build_required

    app.dependency_overrides[_build_required] = fake_session
    app.dependency_overrides[current_mapping] = fake_mapping

    @app.get("/admin")
    async def admin_route(
        handle: ClickHouseAdminHandle = Depends(require_clickhouse_admin),
    ) -> dict[str, Any]:
        return {"ok": True, "username": handle._username}

    # Install exception handlers so AuthForbidden / AuthorizationMisconfigured
    # produce the right HTTP status.
    from iris.auth.exceptions import install_exception_handlers
    # Mock templates to avoid TemplateResponse needing a real templates state.
    app.state.templates = MagicMock()
    install_exception_handlers(app, cookie_name="iris_session")
    return app


def test_require_clickhouse_admin_500s_when_role_missing_from_yaml() -> None:
    app = _admin_app(_mapping_without_admin_role(), frozenset())
    response = TestClient(app, raise_server_exceptions=False).get("/admin")
    assert response.status_code == 500


def test_require_clickhouse_admin_403s_when_user_lacks_role() -> None:
    app = _admin_app(_mapping_with_admin_role(), frozenset({"reader"}))
    response = TestClient(app).get(
        "/admin", headers={"accept": "application/json"}
    )
    assert response.status_code == 403


def test_require_clickhouse_admin_returns_admin_handle_on_success() -> None:
    app = _admin_app(
        _mapping_with_admin_role(), frozenset({CLICKHOUSE_ADMIN_ROLE})
    )
    response = TestClient(app).get("/admin")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "username": "alice"}
```

- [ ] **Step 5.2: Run the tests to verify they fail**

```
uv run pytest tests/clickhouse/test_clickhouse_deps.py -v
```
Expected: FAIL — `require_clickhouse_admin` not yet defined.

- [ ] **Step 5.3: Implement `require_clickhouse_admin`**

Append to `src/iris/clickhouse/deps.py`:

```python
async def require_clickhouse_admin(
    request: Request,
    session: Session,
    mapping: CurrentMapping,
) -> ClickHouseAdminHandle:
    """Return an admin-handle. 403 unless the user has ``clickhouse_admin``.
    500 if ``clickhouse_admin`` is not defined in the role mapping."""
    if CLICKHOUSE_ADMIN_ROLE not in mapping.roles:
        raise AuthorizationMisconfigured(CLICKHOUSE_ADMIN_ROLE)
    if CLICKHOUSE_ADMIN_ROLE not in session.roles:
        raise AuthForbidden(
            needed=(CLICKHOUSE_ADMIN_ROLE,),
            have=tuple(sorted(session.roles)),
        )
    return ClickHouseAdminHandle(
        client=request.app.state.clickhouse_client,
        username=session.user.username,
        settings=request.app.state.clickhouse_settings,
    )
```

- [ ] **Step 5.4: Run the tests to verify they pass**

```
uv run pytest tests/clickhouse/test_clickhouse_deps.py -v
```
Expected: PASS.

- [ ] **Step 5.5: Type-check**

```
uv run basedpyright --level error
```
Expected: no errors.

- [ ] **Step 5.6: Commit**

```
git add src/iris/clickhouse/deps.py tests/clickhouse/test_clickhouse_deps.py
git commit -m "feat(clickhouse): require_clickhouse_admin dep gates routes on the role

Mirrors require_role semantics: 500 if the role isn't defined in the
YAML, 403 if the user lacks it. On success returns a ClickHouseAdminHandle."
```

---

## Task 6: `iris.clickhouse.install(app)` and public surface

**Files:**
- Create: `src/iris/clickhouse/install.py`
- Modify: `src/iris/clickhouse/__init__.py`
- Test: `tests/clickhouse/test_install.py` (NEW)

`install(app)` builds the shared client, runs `ensure_service_admin`, stores client/settings on `app.state`, and appends a `_provision_on_login` hook to `app.state.post_login_hooks`. It assumes the caller has already run `iris.auth.install(app)` (so `post_login_hooks` exists).

- [ ] **Step 6.1: Write the failing test**

Create `tests/clickhouse/test_install.py`:

```python
"""install(app) wires the ClickHouse client into the FastAPI app and registers
a provisioning hook on the auth post-login list."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI

from iris.auth.identity import User
from iris.clickhouse.install import install
from iris.clickhouse.users import USER_ROLE_SUFFIX, GROUP_ROLE_SUFFIX


def test_install_populates_app_state(ch_settings, ch_container) -> None:
    app = FastAPI()
    app.state.post_login_hooks = []

    install(app)

    assert app.state.clickhouse_client is not None
    assert app.state.clickhouse_settings is not None
    assert len(app.state.post_login_hooks) == 1


def test_install_appends_to_existing_hooks(ch_settings) -> None:
    app = FastAPI()

    async def existing_hook(_user: User) -> None:
        pass

    app.state.post_login_hooks = [existing_hook]
    install(app)

    assert len(app.state.post_login_hooks) == 2
    assert app.state.post_login_hooks[0] is existing_hook


def test_install_hook_calls_init_user_rights(ch_settings, prefix) -> None:
    """The provisioning hook actually creates the user/role/grants in CH."""
    app = FastAPI()
    app.state.post_login_hooks = []
    install(app)

    user = User(
        subject=f"mock:{prefix}_alice",
        username=f"{prefix}_alice",
        display_name="Alice",
        groups=(f"{prefix}_admins",),
    )

    hook = app.state.post_login_hooks[0]
    asyncio.run(hook(user))

    client = app.state.clickhouse_client
    rows = list(
        client.query(
            "SELECT name FROM system.users WHERE name = {u:String}",
            parameters={"u": user.username},
        ).named_results()
    )
    assert len(rows) == 1, rows

    role_rows = list(
        client.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": user.username},
        ).named_results()
    )
    role_names = {r["granted_role_name"] for r in role_rows}
    assert f"{user.username}{USER_ROLE_SUFFIX}" in role_names
    assert f"{prefix}_admins{GROUP_ROLE_SUFFIX}" in role_names


def test_install_fails_loud_when_ensure_service_admin_fails(monkeypatch) -> None:
    """If CH is unreachable, install() raises and build_app refuses to boot."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_PORT", "1")  # closed port
    monkeypatch.setenv("CLICKHOUSE_USER", "iris_svc")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "x")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "false")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "iris_svc")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")

    app = FastAPI()
    app.state.post_login_hooks = []
    with pytest.raises(Exception):
        install(app)
```

- [ ] **Step 6.2: Run the tests to verify they fail**

```
uv run pytest tests/clickhouse/test_install.py -v
```
Expected: FAIL — `iris.clickhouse.install` doesn't exist.

- [ ] **Step 6.3: Implement `install`**

Create `src/iris/clickhouse/install.py`:

```python
"""Wire iris.clickhouse into a FastAPI app.

Builds the shared clickhouse-connect Client, runs ensure_service_admin (idempotent),
stashes client + settings on app.state, and registers a post-login provisioning
hook so init_user_rights fires once per real authentication.

The caller MUST have already called iris.auth.install(app) so app.state.post_login_hooks
exists. build_app() in iris.app enforces that order.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from iris.auth.identity import User
from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.users import init_user_rights

logger = logging.getLogger("iris.clickhouse")


def install(app: FastAPI) -> None:
    settings = ClickHouseSettings.from_env()
    client = build_client(settings)
    ensure_service_admin(client, settings)

    app.state.clickhouse_client = client
    app.state.clickhouse_settings = settings

    async def _provision_on_login(user: User) -> None:
        await asyncio.to_thread(
            init_user_rights,
            client,
            username=user.username,
            groups=list(user.groups),
            settings=settings,
        )
        logger.info(
            "clickhouse: provisioned user=%s groups=%s",
            user.username,
            list(user.groups),
        )

    if not hasattr(app.state, "post_login_hooks"):
        app.state.post_login_hooks = []
    app.state.post_login_hooks.append(_provision_on_login)
```

- [ ] **Step 6.4: Update the package's public surface**

Modify `src/iris/clickhouse/__init__.py` to add the new exports:

```python
from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.deps import (
    CLICKHOUSE_ADMIN_ROLE,
    get_clickhouse_handle,
    require_clickhouse_admin,
)
from iris.clickhouse.grants import (
    grant_insert_update_to_table,
    grant_select_to_database,
)
from iris.clickhouse.handle import ClickHouseAdminHandle, ClickHouseHandle
from iris.clickhouse.install import install
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.users import init_user_rights

__all__ = [
    "CLICKHOUSE_ADMIN_ROLE",
    "ClickHouseAdminHandle",
    "ClickHouseHandle",
    "ClickHouseSettings",
    "add_row_policy",
    "build_client",
    "ensure_service_admin",
    "get_clickhouse_handle",
    "grant_insert_update_to_table",
    "grant_select_to_database",
    "init_user_rights",
    "install",
    "require_clickhouse_admin",
    "revoke_row_policy",
    "role_grants",
    "role_row_policies",
    "table_row_policies",
    "user_grants",
    "user_role_memberships",
    "user_row_policies",
]
```

- [ ] **Step 6.5: Run the tests to verify they pass**

```
uv run pytest tests/clickhouse/test_install.py -v
```
Expected: PASS.

- [ ] **Step 6.6: Type-check**

```
uv run basedpyright --level error
```
Expected: no errors.

- [ ] **Step 6.7: Commit**

```
git add src/iris/clickhouse/install.py src/iris/clickhouse/__init__.py tests/clickhouse/test_install.py
git commit -m "feat(clickhouse): install(app) wires the bridge into a FastAPI app

Builds the shared client, runs ensure_service_admin, stashes
client+settings on app.state, and appends a post-login hook that
provisions the CH user/groups via init_user_rights."
```

---

## Task 7: Wire `iris.clickhouse.install` into `build_app`

**Files:**
- Modify: `src/iris/app.py`
- Modify: `tests/conftest.py`

The default `build_app()` now calls `iris.clickhouse.install(app)` after `install_auth(app)`. The `app` fixture in `tests/conftest.py` opts out via `install_clickhouse=False` so non-CH auth tests don't need a CH testcontainer.

- [ ] **Step 7.1: Modify `build_app`**

In `src/iris/app.py`, change the function:

```python
def build_app(*, install_clickhouse: bool = True) -> FastAPI:
    app = FastAPI(title="Iris", lifespan=_lifespan)

    from iris.auth.routes import install as install_auth

    install_auth(app)

    if install_clickhouse:
        from iris.clickhouse.install import install as install_clickhouse_fn
        install_clickhouse_fn(app)

    app.add_middleware(SecurityHeadersMiddleware)
    ...
```

The module-level `app = build_app()` at the bottom of `src/iris/app.py` will fail at import time if `CLICKHOUSE_*` env vars aren't set OR CH is unreachable. That's the production fail-loud posture. For local dev without CH, set `IRIS_NO_CLICKHOUSE=1` (NEW env var, optional). Add this safety:

```python
import os
app = build_app(install_clickhouse=os.environ.get("IRIS_NO_CLICKHOUSE") != "1")
```

- [ ] **Step 7.2: Update `tests/conftest.py` `app` fixture**

In `tests/conftest.py`, modify the `app` fixture:

```python
@pytest.fixture
def app():
    from iris.app import build_app

    return build_app(install_clickhouse=False)
```

Also extend the YAML fixture string to define `clickhouse_admin`:

```python
_AUTHZ_FIXTURE = """\
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: []
    users: []
    includes: ["reader"]
  admin:
    groups: ["admins"]
    users: []
    includes: ["writer"]
  clickhouse_admin:
    groups: ["admins"]
    users: []
"""
```

(`clickhouse_admin` is independent of `admin`; operators decide whether `admin` includes `clickhouse_admin`. The fixture maps both to the `admins` IdP group so the mock alice gets both roles.)

- [ ] **Step 7.3: Run the auth suite to verify no regressions**

```
uv run pytest tests/auth -x
```
Expected: PASS.

- [ ] **Step 7.4: Run the clickhouse unit tier (no testcontainer needed)**

```
uv run pytest tests/clickhouse/test_handle.py tests/clickhouse/test_clickhouse_deps.py -x
```
Expected: PASS.

- [ ] **Step 7.5: Type-check**

```
uv run basedpyright --level error
```
Expected: no errors.

- [ ] **Step 7.6: Commit**

```
git add src/iris/app.py tests/conftest.py
git commit -m "feat(app): build_app installs the ClickHouse bridge by default

Add install_clickhouse param (default True) so production wiring is
unchanged but auth tests can opt out. tests/conftest.py app fixture
opts out; the YAML fixture gains a clickhouse_admin role."
```

---

## Task 8: Integration test — `EXECUTE AS` works end-to-end

**Files:**
- Create: `tests/clickhouse/test_handle_integration.py`

Verify against the real CH testcontainer that:
1. `query_as_user` actually impersonates — `EXECUTE AS alice SELECT user()` returns `alice`.
2. `query_as_service` does not impersonate — `SELECT user()` returns the service-admin user.
3. Provisioning a fresh user and then impersonating them through the handle works.

- [ ] **Step 8.1: Write the integration tests**

Create `tests/clickhouse/test_handle_integration.py`:

```python
"""Integration tests: EXECUTE AS prefix actually impersonates against a real CH server."""
from __future__ import annotations

import pytest

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.handle import ClickHouseAdminHandle, ClickHouseHandle
from iris.clickhouse.users import init_user_rights


@pytest.mark.asyncio
async def test_query_as_user_impersonates(ch_client, ch_settings, prefix) -> None:
    username = f"{prefix}_alice"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    handle = ClickHouseHandle(client=ch_client, username=username)
    result = await handle.query_as_user("SELECT user() AS u")

    rows = list(result.named_results())
    assert rows == [{"u": username}], rows


@pytest.mark.asyncio
async def test_query_as_service_does_not_impersonate(
    ch_client, ch_settings, prefix
) -> None:
    handle = ClickHouseAdminHandle(
        client=ch_client, username=f"{prefix}_unused", settings=ch_settings
    )
    result = await handle.query_as_service("SELECT user() AS u")

    rows = list(result.named_results())
    assert rows == [{"u": ch_settings.user}], rows


@pytest.mark.asyncio
async def test_admin_handle_query_as_user_still_impersonates(
    ch_client, ch_settings, prefix
) -> None:
    username = f"{prefix}_admin"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    handle = ClickHouseAdminHandle(
        client=ch_client, username=username, settings=ch_settings
    )
    result = await handle.query_as_user("SELECT user() AS u")

    rows = list(result.named_results())
    assert rows == [{"u": username}], rows


@pytest.mark.asyncio
async def test_query_as_user_passes_parameters(
    ch_client, ch_settings, prefix
) -> None:
    username = f"{prefix}_paramuser"
    init_user_rights(ch_client, username=username, groups=[], settings=ch_settings)

    handle = ClickHouseHandle(client=ch_client, username=username)
    result = await handle.query_as_user(
        "SELECT {x:Int32} AS v", parameters={"x": 42}
    )

    rows = list(result.named_results())
    assert rows == [{"v": 42}], rows
```

- [ ] **Step 8.2: Run the integration tests**

```
uv run pytest tests/clickhouse/test_handle_integration.py -v
```
Expected: PASS. If `EXECUTE AS x SELECT ...` is not accepted by `client.query()` over HTTP, this is where you discover it. Fall-back: if needed, change the impersonation construction in `handle.py` to use `client.command()` for the prefix and `client.query()` for the body, sharing a `session_id` parameter so the EXECUTE AS persists for the SELECT (then re-issue `EXECUTE AS service_admin` to clear). Re-run until tests pass.

- [ ] **Step 8.3: Commit**

```
git add tests/clickhouse/test_handle_integration.py
git commit -m "test(clickhouse): EXECUTE AS impersonation works end-to-end

Verifies query_as_user runs under the impersonated identity, query_as_service
runs as the service admin, and parameters survive the EXECUTE AS prefix."
```

---

## Task 9: Bridge test — login provisions the CH user

**Files:**
- Create: `tests/clickhouse/test_login_provisioning.py`

End-to-end: build a real `app` with `install_clickhouse=True`, drive a form-login through `TestClient`, and verify the CH user / `_GRP` memberships were created. Also verify a second login reconciles changed groups, and that CH being unreachable causes login to 500.

- [ ] **Step 9.1: Write the bridge tests**

Create `tests/clickhouse/test_login_provisioning.py`:

```python
"""Bridge tests: form-login through TestClient triggers init_user_rights."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from iris.app import build_app
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX


def _login(client: TestClient, *, username: str, password: str) -> None:
    client.get("/login")
    csrf = client.cookies.get("iris_csrf")
    assert csrf is not None
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "next": "/",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text


def test_form_login_provisions_user_in_clickhouse(
    ch_settings, monkeypatch, prefix
) -> None:
    # Mock provider creds — make alice's username unique per test so the
    # session-scoped CH container doesn't see leftover state.
    username = f"{prefix}_alice"
    monkeypatch.setenv("MOCK_USERNAME", username)
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("MOCK_GROUPS", f"{prefix}_admins")
    monkeypatch.setenv("MOCK_DISPLAY_NAME", "Alice")

    app = build_app(install_clickhouse=True)
    client = TestClient(app)
    _login(client, username=username, password="secret")

    ch = app.state.clickhouse_client
    user_rows = list(
        ch.query(
            "SELECT name FROM system.users WHERE name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )
    assert len(user_rows) == 1, user_rows

    role_rows = list(
        ch.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )
    names = {r["granted_role_name"] for r in role_rows}
    assert f"{username}{USER_ROLE_SUFFIX}" in names
    assert f"{prefix}_admins{GROUP_ROLE_SUFFIX}" in names


def test_second_login_reconciles_group_change(
    ch_settings, monkeypatch, prefix
) -> None:
    username = f"{prefix}_bob"
    monkeypatch.setenv("MOCK_USERNAME", username)
    monkeypatch.setenv("MOCK_PASSWORD", "secret")
    monkeypatch.setenv("MOCK_GROUPS", f"{prefix}_a")
    monkeypatch.setenv("MOCK_DISPLAY_NAME", "Bob")

    app = build_app(install_clickhouse=True)
    client = TestClient(app)
    _login(client, username=username, password="secret")

    # Second login with a different group; rebuild the app so MOCK_GROUPS
    # is re-read by the provider factory.
    monkeypatch.setenv("MOCK_GROUPS", f"{prefix}_b")
    app2 = build_app(install_clickhouse=True)
    client2 = TestClient(app2)
    _login(client2, username=username, password="secret")

    ch = app2.state.clickhouse_client
    role_rows = list(
        ch.query(
            "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
            parameters={"u": username},
        ).named_results()
    )
    names = {r["granted_role_name"] for r in role_rows}
    assert f"{prefix}_b{GROUP_ROLE_SUFFIX}" in names
    assert f"{prefix}_a{GROUP_ROLE_SUFFIX}" not in names


def test_login_fails_loud_when_clickhouse_unreachable(monkeypatch) -> None:
    """If init_user_rights raises during the post-login hook, login itself 500s."""
    monkeypatch.setenv("CLICKHOUSE_HOST", "127.0.0.1")
    monkeypatch.setenv("CLICKHOUSE_PORT", "1")
    monkeypatch.setenv("CLICKHOUSE_USER", "iris_svc")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "x")
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "false")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "iris_svc")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")

    # ensure_service_admin will fail at install time, not at login.
    with pytest.raises(Exception):
        build_app(install_clickhouse=True)
```

Note on the third test: `ensure_service_admin` runs at install, so the test asserts that `build_app` itself raises — the spec says "If CH is unreachable at boot, ensure_service_admin raises and the app refuses to start." A separate scenario where CH goes down after boot but before a login is harder to simulate cleanly and isn't part of the spec's testing requirements; skip it.

- [ ] **Step 9.2: Run the bridge tests**

```
uv run pytest tests/clickhouse/test_login_provisioning.py -v
```
Expected: PASS.

- [ ] **Step 9.3: Run the full clickhouse suite**

```
uv run pytest tests/clickhouse -x
```
Expected: PASS.

- [ ] **Step 9.4: Commit**

```
git add tests/clickhouse/test_login_provisioning.py
git commit -m "test(clickhouse): form-login provisions the user in CH end-to-end

Drives a real form-login through TestClient against the testcontainer
and verifies the user/role/group memberships exist after login.
Also verifies group reconcile across two logins and that ensure_service_admin
fails build_app when CH is unreachable at boot."
```

---

## Task 10: Update CLAUDE.md and run final checks

**Files:**
- Modify: `CLAUDE.md`

Document the new public surface so future contributors discover it through the same channel as the rest of the project.

- [ ] **Step 10.1: Update the CLAUDE.md ClickHouse section**

In `CLAUDE.md`, under the `## ClickHouse` heading, add a new sub-section between "Public surface" and "Conventions":

```markdown
### Auth ↔ ClickHouse bridge

Routes that need to query ClickHouse declare one of two FastAPI deps:

```python
from iris.clickhouse import (
    ClickHouseHandle, ClickHouseAdminHandle,
    get_clickhouse_handle, require_clickhouse_admin,
)

@app.get("/click-user")
async def click_user(handle: ClickHouseHandle = Depends(get_clickhouse_handle)):
    result = await handle.query_as_user("SELECT count() FROM orders.lines")
    ...

@app.get("/click-admin")
async def click_admin(handle: ClickHouseAdminHandle = Depends(require_clickhouse_admin)):
    return await handle.user_grants(username="alice")
```

`get_clickhouse_handle` admits any logged-in user; the handle exposes
`query_as_user` only, which prepends `EXECUTE AS <session_user>` to the SQL.
`require_clickhouse_admin` 403s users without the `clickhouse_admin` role
(and 500s if the role isn't defined in the YAML); on success it returns a
`ClickHouseAdminHandle` that adds `query_as_service` (no impersonation), the
`grant_*`/`add_row_policy`/`revoke_row_policy` mutators, and the audit
helpers (`user_grants`, `role_grants`, `user_row_policies`, ...). Admin
routes that want a user-impersonated query can still call
`handle.query_as_user(...)` on the admin handle.

`init_user_rights` runs on every successful login (form submit or OAuth
callback) via a generic post-login hook list at `app.state.post_login_hooks`,
populated by `iris.clickhouse.install(app)`. Subsequent cookie-based session
refreshes do NOT re-provision. Group changes between two logins are
reconciled. If ClickHouse is unreachable, login itself returns 500.

`build_app(install_clickhouse=False)` skips the bridge entirely — used by
auth tests that don't need a CH testcontainer. Set `IRIS_NO_CLICKHOUSE=1`
to disable the bridge in the module-level production app.
```

- [ ] **Step 10.2: Run the full test suite**

```
uv run pytest -x
```
Expected: PASS.

- [ ] **Step 10.3: Run basedpyright at error and warning levels**

```
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: no errors, no warnings.

- [ ] **Step 10.4: Commit**

```
git add CLAUDE.md
git commit -m "docs: document the auth ↔ ClickHouse bridge in CLAUDE.md

Public surface (handle classes, deps, install), the post-login hook
seam, and the install_clickhouse=False / IRIS_NO_CLICKHOUSE=1 escape
hatches for tests and CH-less local dev."
```

---

## Self-review (run before invoking executing-plans)

Spec coverage:

- [x] Two handle types — Tasks 2 & 3.
- [x] Two FastAPI deps — Tasks 4 & 5.
- [x] `clickhouse_admin` modeled as a regular role; `Final` constant — Task 4.
- [x] Shared app-scoped Client; per-request handle wrapper — Task 6.
- [x] `EXECUTE AS` per-query (form 2) — Task 2 + integration verification in Task 8.
- [x] `init_user_rights` fires once per real authentication; fail-loud — Task 1 (seam) + Task 6 (hook) + Task 9 (verification).
- [x] `ensure_service_admin` fails at boot — Task 6 + Task 9.
- [x] Generic post-login hook list on `app.state` — Task 1.
- [x] `build_app(install_clickhouse=...)` parameter for test opt-out — Task 1 (param) + Task 7 (wiring).
- [x] Module layout (handle.py, deps.py, install.py) — Tasks 2-6.
- [x] Public surface re-exports — Task 6.
- [x] Three test layers (unit / integration / bridge) — Tasks 2-3 (unit), 8 (integration), 9 (bridge).
- [x] Auth tests untouched — Task 7 (`app` fixture opts out).

No placeholder text. Method signatures are consistent across tasks (`username` keyword on audit/admin methods; positional `sql` first; `parameters` keyword). `CLICKHOUSE_ADMIN_ROLE` is a `Final` and used uniformly.
