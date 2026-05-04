# Session API Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the `iris.auth` public surface from five overlapping deps + two value types to two deps + two value types organized around a single request-scoped `Session` view.

**Architecture:** Additive build-out (Tasks 1–3) → atomic `require_role` shape change with all callers (Task 4) → caller migrations (Tasks 5–7) → cleanup of old API (Tasks 8–11) → docs and gates (Tasks 12–13). Each task ends green; no intermediate broken state.

**Tech Stack:** FastAPI 0.136, Python 3.13, pytest with `--import-mode=importlib`, basedpyright (zero-error & zero-warning gates), ruff.

**Reference:** [Design spec](../specs/2026-05-04-session-api-simplification-design.md) — read first if you want context for any decision.

---

## File Structure

| Path | Action | Purpose |
|------|--------|---------|
| `src/iris/auth/session.py` | create | `Session` frozen dataclass (request-scoped view). |
| `src/iris/auth/authz/core.py` | create | `resolve_roles`, `current_mapping`, `CurrentMapping` extracted from `authz/deps.py` (renamed from underscore-prefixed forms; package-internal but cross-module so no underscore). |
| `src/iris/auth/deps.py` | rewrite | Drop 4 surface aliases; add `Session`/`OptionalSession`. Keep `_resolve_stored` (renamed from `_resolve_session`), `set_session_store`, `set_settings`. |
| `src/iris/auth/authz/deps.py` | slim | Only `require_role` remains; it depends on `Session` and returns Session. |
| `src/iris/auth/__init__.py` | rewrite | New `__all__` of 5 names: `Session`, `OptionalSession`, `User`, `install`, `require_role`. |
| `src/iris/auth/routes.py` | edit | `logout` and `whoami` route signatures: `CurrentUser` → `Session`. |
| `src/iris/app.py` | edit | 3 routes (`index`, `greet`, `clock`): `CurrentUser` → `Session`. |
| `tests/auth/test_session_dep.py` | create | Comprehensive Session/OptionalSession dep coverage. |
| `tests/auth/test_deps.py` | delete | Coverage migrated into `test_session_dep.py`. |
| `tests/auth/authz/test_authz_deps.py` | edit | `require_role` route annotations → Session; `CurrentRoles` tests → `session.roles`. |
| `tests/auth/test_error_pages.py` | edit | `require_role` annotation → Session. |
| `CLAUDE.md` | edit | Auth section + module map. |

Files **unchanged**: `src/iris/auth/identity.py` (UserSession stays as-is, just no longer publicly re-exported), `src/iris/auth/sessions.py`, `src/iris/auth/exceptions.py`, `src/iris/auth/csrf.py`, `src/iris/auth/rate_limit.py`, `src/iris/auth/providers/*`, `src/iris/auth/authz/{config,loader,mapping}.py`, `tests/conftest.py` (the `authed_client` fixture imports `User` directly from `iris.auth.identity`, which is unaffected).

---

## Task 1: Create the `Session` value type

**Files:**
- Create: `src/iris/auth/session.py`

This task is purely additive: a new module that nothing yet imports. The new file is exercised by tests in Task 3.

- [ ] **Step 1: Create the Session module**

Write `src/iris/auth/session.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from iris.auth.identity import User


@dataclass(frozen=True, slots=True)
class Session:
    """Request-scoped view of a logged-in session.

    Built once per request by the auth dep. Routes receive it via the
    ``Session`` or ``OptionalSession`` annotated aliases from
    ``iris.auth.deps``.

    Frozen except for ``data``, which is the SAME ``dict`` object as
    ``UserSession.data`` in the session store. This means
    ``session.data[key] = value`` writes through to the store with no
    commit step. All other fields are immutable from the route's view.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    roles: frozenset[str]
```

- [ ] **Step 2: Verify the package still imports**

Run: `uv run python -c "import iris.auth.session; print(iris.auth.session.Session)"`
Expected: `<class 'iris.auth.session.Session'>`

- [ ] **Step 3: Run the existing test suite to confirm nothing regressed**

Run: `uv run pytest -q`
Expected: all tests still pass (the new module isn't imported by anything yet).

- [ ] **Step 4: Commit**

```bash
git add src/iris/auth/session.py
git commit -m "feat(auth): add Session frozen-dataclass view type"
```

---

## Task 2: Extract authz helpers to `authz/core.py`

**Files:**
- Create: `src/iris/auth/authz/core.py`
- Modify: `src/iris/auth/authz/deps.py`

**Why:** The new `iris.auth.deps._build_optional` will need the role-resolution and current-mapping helpers. Today those live in `iris.auth.authz.deps`, which already imports from `iris.auth.deps` (`CurrentUser`). Moving them down a layer breaks the cycle. While moving them, drop the underscore prefixes — `_resolve_roles` → `resolve_roles`, `_current_mapping` → `current_mapping`, `_CurrentMapping` → `CurrentMapping` — because they cross module boundaries within the package and the `_` prefix would trip basedpyright's `reportPrivateUsage` on every import.

This task is a pure code move; no behavior change. Existing tests must stay green.

**Naming note.** When these helpers lived inside `authz/deps.py` they were prefixed with `_` because they were module-private. After the move, they cross module boundaries within the package, so the Pythonic convention is to drop the underscore: `resolve_roles`, `current_mapping`, `CurrentMapping`. They stay out of any `__all__`, which keeps them package-internal in spirit. (Leaving the underscore would trip basedpyright's `reportPrivateUsage` on every cross-module import.)

- [ ] **Step 1: Create `src/iris/auth/authz/core.py`**

```python
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from iris.auth.authz.mapping import RoleMapping
from iris.auth.identity import User


def resolve_roles(user: User, mapping: RoleMapping) -> frozenset[str]:
    base: set[str] = set()
    username_lower = user.username.lower()
    user_groups = set(user.groups)
    for role_name, role_def in mapping.roles.items():
        if username_lower in role_def.users_lower:
            base.add(role_name)
        elif role_def.groups & user_groups:
            base.add(role_name)
    effective: set[str] = set()
    for r in base:
        effective |= mapping.closure[r]
    return frozenset(effective)


async def current_mapping(request: Request) -> RoleMapping:
    return request.app.state.authz_loader.get()


CurrentMapping = Annotated[RoleMapping, Depends(current_mapping)]
```

- [ ] **Step 2: Update `src/iris/auth/authz/deps.py` to import from core**

Replace the file's contents with:

```python
from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from iris.auth.authz.core import CurrentMapping, resolve_roles
from iris.auth.deps import CurrentUser
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.identity import User


async def _current_roles(mapping: CurrentMapping, user: CurrentUser) -> frozenset[str]:
    return resolve_roles(user, mapping)


CurrentRoles = Annotated[frozenset[str], Depends(_current_roles)]


def require_role(role: str):
    async def _check(
        mapping: CurrentMapping,
        roles: CurrentRoles,
        user: CurrentUser,
    ) -> User:
        if role not in mapping.roles:
            raise AuthorizationMisconfigured(role)
        if role not in roles:
            raise AuthForbidden(needed=(role,), have=tuple(sorted(roles)))
        return user

    return _check
```

The behavior is identical to the previous version — only the helpers' definitions moved (and lost their underscore prefixes for cross-module use). `CurrentRoles`, `_current_roles`, `require_role` retain their old shape (still returning `User`); they'll change in Task 4 / Task 10. The `current_mapping` *function* is referenced via `Depends(current_mapping)` baked into the `CurrentMapping` Annotated alias, so `authz/deps.py` only imports the alias.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass — including `tests/auth/authz/test_authz_deps.py`, which still imports `CurrentRoles` and `require_role` from `iris.auth.authz.deps`.

- [ ] **Step 4: Commit**

```bash
git add src/iris/auth/authz/core.py src/iris/auth/authz/deps.py
git commit -m "refactor(auth): extract role-mapping helpers to authz/core.py"
```

---

## Task 3: Add `Session` and `OptionalSession` deps with TDD

**Files:**
- Create: `tests/auth/test_session_dep.py`
- Modify: `src/iris/auth/deps.py`
- Modify: `src/iris/auth/__init__.py`

This task introduces the new public deps alongside the old ones. Old aliases stay; this is an additive change. TDD discipline: write the failing test file first, watch it fail, then add the implementation, watch it pass.

- [ ] **Step 1: Write the failing test file**

Create `tests/auth/test_session_dep.py`:

```python
import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris.auth import Session, OptionalSession
from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.sessions import InMemorySessionStore


_FIXTURE_YAML = """
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: ["editors"]
    users: []
    includes: ["reader"]
  admin:
    groups: ["admins"]
    users: []
    includes: ["writer"]
"""


def _build_app(tmp_path: Path) -> tuple[FastAPI, InMemorySessionStore]:
    yaml_path = tmp_path / "authz.yaml"
    yaml_path.write_text(_FIXTURE_YAML)

    app = FastAPI()
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=3600)
    set_session_store(app, store)
    set_settings(app, cookie_name="iris_session")
    install_exception_handlers(app, cookie_name="iris_session")
    app.state.authz_loader = RoleMappingLoader(yaml_path)

    @app.get("/me")
    async def me(session: Session):
        return {"subject": session.user.subject}

    @app.get("/optional")
    async def optional(session: OptionalSession):
        return {"present": session is not None}

    @app.get("/whoami-full")
    async def whoami_full(session: Session):
        return {
            "id": session.id,
            "subject": session.user.subject,
            "data_keys": sorted(session.data.keys()),
            "roles": sorted(session.roles),
        }

    @app.get("/data")
    async def read_data(session: Session):
        return {"counter": session.data.get("counter", 0)}

    @app.post("/data")
    async def bump_data(session: Session):
        session.data["counter"] = session.data.get("counter", 0) + 1
        return {"counter": session.data["counter"]}

    return app, store


def _seed(store: InMemorySessionStore, **overrides) -> str:
    user = User(
        subject=overrides.get("subject", "alice"),
        username=overrides.get("username", overrides.get("subject", "alice")),
        display_name=overrides.get("display_name", "Alice"),
        groups=overrides.get("groups", ("admins",)),
    )
    session = asyncio.run(store.create(user))
    return session.id


def test_no_credentials_returns_401(tmp_path):
    app, _ = _build_app(tmp_path)
    r = TestClient(app).get("/me", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_cookie_credential_resolves_session(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/me", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"subject": "alice"}


def test_bearer_credential_resolves_session(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store)
    r = TestClient(app).get(
        "/me",
        headers={"accept": "application/json", "authorization": f"Bearer {sid}"},
    )
    assert r.status_code == 200
    assert r.json() == {"subject": "alice"}


def test_optional_session_returns_none_when_unauthenticated(tmp_path):
    app, _ = _build_app(tmp_path)
    r = TestClient(app).get("/optional", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"present": False}


def test_optional_session_returns_session_when_authenticated(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/optional", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"present": True}


def test_session_data_round_trip(tmp_path):
    """Mutations to session.data persist across requests with the same session id."""
    app, store = _build_app(tmp_path)
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    assert c.get("/data").json() == {"counter": 0}
    assert c.post("/data").json() == {"counter": 1}
    assert c.post("/data").json() == {"counter": 2}
    assert c.get("/data").json() == {"counter": 2}


def test_session_data_isolated_between_sessions(tmp_path):
    """Two sessions don't see each other's data."""
    app, store = _build_app(tmp_path)
    sid_a = _seed(store, subject="alice")
    sid_b = _seed(store, subject="bob")
    ca = TestClient(app)
    ca.cookies.set("iris_session", sid_a)
    cb = TestClient(app)
    cb.cookies.set("iris_session", sid_b)
    ca.post("/data")
    ca.post("/data")
    cb.post("/data")
    assert ca.get("/data").json() == {"counter": 2}
    assert cb.get("/data").json() == {"counter": 1}


def test_session_data_requires_auth(tmp_path):
    """Without a session cookie or bearer, /data 401s like Session always does."""
    app, _ = _build_app(tmp_path)
    r = TestClient(app).get("/data", headers={"accept": "application/json"})
    assert r.status_code == 401


def test_session_exposes_id_user_and_data(tmp_path):
    """The Session view exposes id, user, and data."""
    app, store = _build_app(tmp_path)
    sid = _seed(store)
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    c.post("/data")
    r = c.get("/whoami-full", headers={"accept": "application/json"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sid
    assert body["subject"] == "alice"
    assert body["data_keys"] == ["counter"]


def test_session_roles_includes_closure(tmp_path):
    """admin → writer → reader closure surfaces on session.roles."""
    app, store = _build_app(tmp_path)
    sid = _seed(store, subject="charlie", groups=("admins",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/whoami-full", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json()["roles"] == ["admin", "reader", "writer"]


def test_session_roles_empty_for_user_without_match(tmp_path):
    app, store = _build_app(tmp_path)
    sid = _seed(store, subject="dave", groups=("strangers",))
    c = TestClient(app)
    c.cookies.set("iris_session", sid)
    r = c.get("/whoami-full", headers={"accept": "application/json"})
    assert r.status_code == 200
    assert r.json()["roles"] == []
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest tests/auth/test_session_dep.py -q`
Expected: collection error or all-fail with `ImportError: cannot import name 'Session' from 'iris.auth'`.

- [ ] **Step 3: Add the new deps to `src/iris/auth/deps.py`**

Replace the file contents with:

```python
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request

from iris.auth.authz.core import CurrentMapping, resolve_roles
from iris.auth.exceptions import AuthRequired
from iris.auth.identity import User, UserSession
from iris.auth.session import Session as _SessionT
from iris.auth.sessions import InMemorySessionStore


def set_session_store(app: FastAPI, store: InMemorySessionStore) -> None:
    app.state.auth_session_store = store


def set_settings(app: FastAPI, *, cookie_name: str, cookie_secure: bool = True) -> None:
    app.state.auth_cookie_name = cookie_name
    app.state.auth_cookie_secure = cookie_secure


def _get_store(request: Request) -> InMemorySessionStore:
    return request.app.state.auth_session_store


def _get_cookie_name(request: Request) -> str:
    return request.app.state.auth_cookie_name


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def _resolve_session(request: Request) -> UserSession | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name) or _bearer(
        request.headers.get("authorization")
    )
    if not sid:
        return None
    store = _get_store(request)
    return await store.get_and_refresh(sid)


_ResolvedSession = Annotated[UserSession | None, Depends(_resolve_session)]


async def _required_session(session: _ResolvedSession) -> UserSession:
    if session is None:
        raise AuthRequired()
    return session


_RequiredSession = Annotated[UserSession, Depends(_required_session)]


# --- Old surface (will be removed in Task 9) ----------------------------------


async def _current_user(session: _RequiredSession) -> User:
    return session.user


async def _optional_current_user(session: _ResolvedSession) -> User | None:
    return session.user if session else None


async def _current_session(session: _RequiredSession) -> UserSession:
    return session


async def _session_data(session: _RequiredSession) -> dict[str, Any]:
    return session.data


CurrentUser = Annotated[User, Depends(_current_user)]
OptionalCurrentUser = Annotated[User | None, Depends(_optional_current_user)]
CurrentSession = Annotated[UserSession, Depends(_current_session)]
SessionData = Annotated[dict[str, Any], Depends(_session_data)]


# --- New surface --------------------------------------------------------------


async def _build_optional(
    stored: _ResolvedSession,
    mapping: CurrentMapping,
) -> _SessionT | None:
    if stored is None:
        return None
    return _SessionT(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        roles=resolve_roles(stored.user, mapping),
    )


_BuiltOptional = Annotated[_SessionT | None, Depends(_build_optional)]


async def _build_required(view: _BuiltOptional) -> _SessionT:
    if view is None:
        raise AuthRequired()
    return view


Session = Annotated[_SessionT, Depends(_build_required)]
OptionalSession = Annotated[_SessionT | None, Depends(_build_optional)]
```

The old block (clearly commented) is unchanged from today; the new block is added below it. The new builder reuses the existing `_resolve_session` dep, so FastAPI's per-request cache still serves both old and new with one store hit.

- [ ] **Step 4: Re-export `Session` and `OptionalSession` from `iris/auth/__init__.py`**

Edit `src/iris/auth/__init__.py` to add the new names:

```python
from iris.auth.authz.deps import CurrentRoles, require_role
from iris.auth.deps import (
    CurrentSession,
    CurrentUser,
    OptionalCurrentUser,
    OptionalSession,
    Session,
    SessionData,
)
from iris.auth.identity import User, UserSession
from iris.auth.routes import install

__all__ = [
    "CurrentRoles",
    "CurrentSession",
    "CurrentUser",
    "OptionalCurrentUser",
    "OptionalSession",
    "Session",
    "SessionData",
    "User",
    "UserSession",
    "install",
    "require_role",
]
```

(Old names stay until Task 11.)

- [ ] **Step 5: Run the new tests**

Run: `uv run pytest tests/auth/test_session_dep.py -q`
Expected: all 11 tests pass.

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass (old + new).

- [ ] **Step 7: Commit**

```bash
git add src/iris/auth/deps.py src/iris/auth/__init__.py tests/auth/test_session_dep.py
git commit -m "feat(auth): add Session and OptionalSession deps"
```

---

## Task 4: Refactor `require_role` to return `Session`

**Files:**
- Modify: `src/iris/auth/authz/deps.py`
- Modify: `tests/auth/authz/test_authz_deps.py` (3 routes)
- Modify: `tests/auth/test_error_pages.py` (1 route)
- Modify: `tests/auth/test_deps.py` (1 route — file is deleted in Task 8 but must stay green here)

This is a behavioral change to `require_role`'s return type, which has 5 call sites across 3 test files. All change atomically in this task. TDD: update the tests first (red), then change `require_role` (green).

- [ ] **FastAPI constraint that shapes the imports.** FastAPI 0.136.1 raises `AssertionError: Cannot specify Depends in Annotated and default value together` for `param: AliasWithDepends = Depends(other)`. The spec's earlier claim that the explicit `=` Depends "overrides" the Annotated alias is wrong (verified empirically). So:

- For bare-auth routes (no role check): import the **alias** from `iris.auth` and write `session: Session` with no `=`.
- For role-gated routes: import the **class** from `iris.auth.session` and write `session: Session = Depends(require_role("..."))`.
- A file mixing both patterns imports the alias under a local name (`RequireSession`, `AuthSession`, etc.) so the two names don't collide.

This task and Tasks 5–7 follow that convention.

**Step 1: Update `tests/auth/authz/test_authz_deps.py` to expect Session**

In `tests/auth/authz/test_authz_deps.py`, replace the import block at lines 1–12 and the three role-gated routes:

Replace the import block with:

```python
import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth.authz.deps import CurrentRoles, require_role
from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.session import Session
from iris.auth.sessions import InMemorySessionStore
```

(Note: imports the **class** from `iris.auth.session`, not the alias from `iris.auth`. Task 7 will add a separately-named local alias for the `my_roles` route, which uses the bare-auth pattern.)

Replace the three role-gated route definitions:

```python
    @app.get("/reader-only")
    async def reader_only(session: Session = Depends(require_role("reader"))):
        return {"subject": session.user.subject}

    @app.get("/admin-only")
    async def admin_only(session: Session = Depends(require_role("admin"))):
        return {"subject": session.user.subject}

    @app.get("/needs-undefined-role")
    async def needs_undefined(session: Session = Depends(require_role("super_admin"))):
        return {"subject": session.user.subject}
```

Leave the `my_roles` route (using `CurrentRoles`) unchanged — it migrates in Task 7.

- [ ] **Step 2: Update `tests/auth/test_error_pages.py` to expect Session**

In `tests/auth/test_error_pages.py`, replace the imports and the `admin_only` route. (This file's only auth-related route is role-gated, so it imports the **class**.)

```python
from fastapi import Depends
from fastapi.testclient import TestClient

from iris.auth.authz.deps import require_role
from iris.auth.session import Session
```

```python
    @app.get("/admin-only")
    async def admin_only(_: Session = Depends(require_role("admin"))):
        return {"ok": True}
```

The unused `User` import goes away. The body is unchanged (`_` is unused).

- [ ] **Step 3: Update `tests/auth/test_deps.py` to expect Session**

In `tests/auth/test_deps.py`, the `admin` route at line 41–43:

```python
    @app.get("/admin")
    async def admin(session: Session = Depends(require_role("admin"))):
        return {"subject": session.user.subject}
```

Add the `Session` **class** import at the top (since this route is role-gated, not bare-auth):

```python
from iris.auth.session import Session
```

(Existing imports of `CurrentUser`, `OptionalCurrentUser`, `CurrentSession`, `SessionData` stay — they're still used by other tests in this file. The whole file is deleted in Task 8.)

- [ ] **Step 4: Run the affected test files — they should fail**

Run: `uv run pytest tests/auth/authz/test_authz_deps.py tests/auth/test_error_pages.py tests/auth/test_deps.py::test_require_role_admits_member tests/auth/test_deps.py::test_require_role_rejects_non_member -q`
Expected: failures. The route signatures now annotate `session: Session`, but `require_role` is still injecting a `User`. Accessing `session.user.subject` on what's actually a `User` raises `AttributeError` (User has no `.user`).

- [ ] **Step 5: Update `require_role` to return Session**

Replace the body of `src/iris/auth/authz/deps.py` with:

```python
from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from iris.auth.authz.core import CurrentMapping, resolve_roles
from iris.auth.deps import CurrentUser, Session
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.session import Session as _SessionT


async def _current_roles(mapping: CurrentMapping, user: CurrentUser) -> frozenset[str]:
    return resolve_roles(user, mapping)


CurrentRoles = Annotated[frozenset[str], Depends(_current_roles)]


def require_role(role: str):
    async def _check(session: Session, mapping: CurrentMapping) -> _SessionT:
        if role not in mapping.roles:
            raise AuthorizationMisconfigured(role)
        if role not in session.roles:
            raise AuthForbidden(
                needed=(role,), have=tuple(sorted(session.roles))
            )
        return session

    return _check
```

Notes:
- `_check` now takes `session: Session` (the Annotated alias from `iris.auth.deps`). FastAPI shares the `Session` resolution across the route's other deps via the per-request cache.
- `_check` returns `_SessionT` (the class, imported under that alias to avoid shadowing the `Session` name we use as a parameter annotation inside this same module).
- `CurrentRoles` and `_current_roles` are kept here unchanged for now — they're removed in Task 10 once the last `CurrentRoles` caller migrates in Task 7.
- `CurrentUser` import stays only because `_current_roles` still uses it. It goes in Task 10.

- [ ] **Step 6: Run the affected test files — they should pass**

Run: `uv run pytest tests/auth/authz/test_authz_deps.py tests/auth/test_error_pages.py tests/auth/test_deps.py -q`
Expected: all tests pass.

- [ ] **Step 7: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/iris/auth/authz/deps.py tests/auth/authz/test_authz_deps.py tests/auth/test_error_pages.py tests/auth/test_deps.py
git commit -m "refactor(auth): require_role now returns Session (not User)"
```

---

## Task 5: Migrate `src/iris/app.py` routes to `Session`

**Files:**
- Modify: `src/iris/app.py`

The 3 application routes (`index`, `greet`, `clock`) take `CurrentUser`. Switch each to `Session`. No template changes — the `index` route still passes a `user` variable to the template, sourced from `session.user`.

- [ ] **Step 1: Replace the imports**

At the top of `src/iris/app.py`, change line 14:

```python
from iris.auth import Session
```

(Remove `from iris.auth.csrf import attach_csrf_cookie, mint_csrf_token` is unchanged.)

- [ ] **Step 2: Update the three routes**

Replace the three route bodies inside `build_app()`:

```python
    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, session: Session):
        # Mint (or reuse) the CSRF token, then attach the cookie to the
        # TemplateResponse explicitly. Routes that return their own Response
        # bypass FastAPI's dep-injected-Response cookie merge, so we can't
        # rely on Depends(issue_csrf_token) here.
        csrf = mint_csrf_token(request)
        response = TEMPLATES.TemplateResponse(
            request, "index.html", {"user": session.user, "csrf_token": csrf}
        )
        attach_csrf_cookie(request, response, csrf)
        return response

    @app.get("/api/greet")
    async def greet(signals: Signals, session: Session) -> DatastarResponse:
        raw = str(signals.get("name") or session.user.display_name).strip()
        name = escape(raw) if raw else "stranger"
        return DatastarResponse(
            SSE.patch_elements(f'<div id="greeting">Hello, <strong>{name}</strong>!</div>')
        )

    @app.get("/api/clock")
    async def clock(_session: Session) -> DatastarResponse:
        return DatastarResponse(_clock_stream())
```

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass. `tests/test_app.py` exercises these routes via `authed_client`; the JSON/HTML responses they return are unchanged.

- [ ] **Step 4: Commit**

```bash
git add src/iris/app.py
git commit -m "refactor(app): migrate routes to Session dep"
```

---

## Task 6: Migrate `src/iris/auth/routes.py` to `Session`

**Files:**
- Modify: `src/iris/auth/routes.py`

Two routes use `CurrentUser`: `logout` and `whoami`. Switch both. Note: imports must come from `iris.auth.deps` directly (not `iris.auth`) to avoid a circular import — `iris/auth/__init__.py` imports `install` from this same module.

- [ ] **Step 1: Replace the import**

In `src/iris/auth/routes.py`, change line 11:

```python
from iris.auth.deps import Session
```

(Drop `from iris.auth.deps import CurrentUser`.)

The line `from iris.auth.identity import User` stays — `_finalize_login_redirect` takes a `user: User` argument from the providers.

- [ ] **Step 2: Update `logout`**

Replace the `logout` route body (currently lines 139–151):

```python
    @router.post("/logout")
    async def logout(
        request: Request,
        session: Session,
        _: None = Depends(verify_csrf_form),
    ) -> Response:
        sid = request.cookies.get(cookie_name) or ""
        if sid:
            await store.delete(sid)
        logger.info(
            "auth: logout user=%s subject=%s",
            session.user.display_name,
            session.user.subject,
        )
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(cookie_name)
        return response
```

- [ ] **Step 3: Update `whoami`**

Replace the `whoami` route body (currently lines 153–159):

```python
    @router.get("/api/whoami")
    async def whoami(session: Session) -> dict[str, Any]:
        return {
            "subject": session.user.subject,
            "display_name": session.user.display_name,
            "groups": list(session.user.groups),
        }
```

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass. The whoami JSON shape and the logout HTTP behavior are unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/iris/auth/routes.py
git commit -m "refactor(auth): migrate auth routes to Session dep"
```

---

## Task 7: Migrate `CurrentRoles` test to `session.roles`

**Files:**
- Modify: `tests/auth/authz/test_authz_deps.py`

The `my_roles` route in this test file uses the now-deprecated `CurrentRoles` dep. Switch it to `Session` and read `session.roles`. After this task, no test imports `CurrentRoles`, clearing the way for Task 10 to remove it.

- [ ] **Step 1: Update the imports**

In `tests/auth/authz/test_authz_deps.py`, the file already imports the `Session` **class** (from Task 4) for the role-gated routes. Add the bare-auth **alias** under a local name so the two patterns can coexist in the same file. Final imports block:

```python
import asyncio
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from iris.auth import Session as RequireSession
from iris.auth.authz.deps import require_role
from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.deps import set_session_store, set_settings
from iris.auth.exceptions import install_exception_handlers
from iris.auth.identity import User
from iris.auth.session import Session
from iris.auth.sessions import InMemorySessionStore
```

(Drops `CurrentRoles` from the `authz.deps` import. Adds `Session as RequireSession` from `iris.auth` — the Annotated alias under a local name so it doesn't collide with the class.)

- [ ] **Step 2: Update the `my_roles` route**

Replace the `my_roles` route in `_build_app`. Use the `RequireSession` alias (so the route doesn't need an explicit `= Depends(...)`):

```python
    @app.get("/my-roles")
    async def my_roles(session: RequireSession):
        return {"roles": sorted(session.roles)}
```

- [ ] **Step 3: Run the affected tests**

Run: `uv run pytest tests/auth/authz/test_authz_deps.py -q`
Expected: all tests pass — including `test_current_roles_returns_full_effective_set_for_admin` and `test_current_roles_returns_empty_set_for_user_with_no_match`, which still assert against `/my-roles` HTTP responses (the JSON shape is unchanged).

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/auth/authz/test_authz_deps.py
git commit -m "test(auth): migrate CurrentRoles test usage to session.roles"
```

---

## Task 8: Delete `tests/auth/test_deps.py`

**Files:**
- Delete: `tests/auth/test_deps.py`

The file's coverage migrated into `tests/auth/test_session_dep.py` in Task 3. The require-role tests overlap with `tests/auth/authz/test_authz_deps.py`. The file is now redundant and references soon-to-be-removed names.

- [ ] **Step 1: Verify coverage parity**

Run: `uv run pytest tests/auth/test_session_dep.py tests/auth/authz/test_authz_deps.py -q`
Expected: 11 + 8 = 19 tests pass. Spot-check that every behavior tested by `test_deps.py` (cookie auth, bearer auth, no-credentials 401, optional-returns-None, optional-returns-user, require_role admit, require_role reject, session-data round-trip, session-data isolation, session-data 401, current-session id+user+data exposed) has a parallel in `test_session_dep.py` or `test_authz_deps.py`.

- [ ] **Step 2: Delete the file**

```bash
git rm tests/auth/test_deps.py
```

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: all remaining tests pass; the test count drops by 12.

- [ ] **Step 4: Commit**

```bash
git commit -m "test(auth): drop test_deps.py (coverage in test_session_dep.py)"
```

---

## Task 9: Update `src/iris/auth/__init__.py` exports

**Files:**
- Modify: `src/iris/auth/__init__.py`

Done **before** the deps.py / authz/deps.py cleanup so there's no intermediate state where `iris.auth` re-exports names that no longer exist downstream. After this task, `iris.auth` exposes only the new five names; the old names still exist in their submodules (orphan code) but are no longer re-exported.

- [ ] **Step 1: Verify no caller imports old names via the package**

Run: `grep -rn -E "from iris\.auth import .*(CurrentUser|OptionalCurrentUser|CurrentSession|SessionData|CurrentRoles|UserSession)" src/ tests/`
Expected: empty. (After Tasks 5–7, no caller routes through `iris.auth` for the old names. The submodules still reference them via direct paths like `iris.auth.deps`; that's fine — those references go away in Tasks 10 and 11.)

- [ ] **Step 2: Replace the file**

Replace the contents of `src/iris/auth/__init__.py` with:

```python
from iris.auth.authz.deps import require_role
from iris.auth.deps import OptionalSession, Session
from iris.auth.identity import User
from iris.auth.routes import install

__all__ = [
    "OptionalSession",
    "Session",
    "User",
    "install",
    "require_role",
]
```

`UserSession` is no longer re-exported. The class still exists at `iris.auth.identity.UserSession` for the session store and any test that needs to reach it directly.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass. The old surface names still exist inside `iris.auth.deps` and `iris.auth.authz.deps`, but the package-level re-exports are gone — and no caller relied on the package-level path anymore.

- [ ] **Step 4: Commit**

```bash
git add src/iris/auth/__init__.py
git commit -m "refactor(auth): finalize __all__ to 5 public names"
```

---

## Task 10: Remove `CurrentRoles` from `src/iris/auth/authz/deps.py`

**Files:**
- Modify: `src/iris/auth/authz/deps.py`

After Task 7, no test imports `CurrentRoles`. After Task 9, no package re-export references it. Drop `_current_roles`, `CurrentRoles`, and the `CurrentUser` / `User` imports they depend on.

This task **must run before** Task 11 because `_current_roles` imports `CurrentUser` from `iris.auth.deps`, and Task 11 removes `CurrentUser`.

- [ ] **Step 1: Verify no caller imports `CurrentRoles` anywhere**

Run: `grep -rn "CurrentRoles" src/ tests/`
Expected: matches only inside `src/iris/auth/authz/deps.py` (the definition this task is about to remove). No matches in any test file or any other production file.

- [ ] **Step 2: Replace `src/iris/auth/authz/deps.py` with the slim version**

```python
from __future__ import annotations

from iris.auth.authz.core import CurrentMapping
from iris.auth.deps import Session
from iris.auth.exceptions import AuthForbidden, AuthorizationMisconfigured
from iris.auth.session import Session as _SessionT


def require_role(role: str):
    async def _check(session: Session, mapping: CurrentMapping) -> _SessionT:
        if role not in mapping.roles:
            raise AuthorizationMisconfigured(role)
        if role not in session.roles:
            raise AuthForbidden(
                needed=(role,), have=tuple(sorted(session.roles))
            )
        return session

    return _check
```

(The `Depends` import is gone — it's only needed to wrap callables, and `_check` itself isn't wrapped at definition time. FastAPI resolves the `Session` and `CurrentMapping` Annotated metadata when `_check` is registered as a dep at route-decoration time.)

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass. `iris.auth.deps.CurrentUser` still exists (not used by anything now); Task 11 removes it.

- [ ] **Step 4: Commit**

```bash
git add src/iris/auth/authz/deps.py
git commit -m "refactor(auth): drop CurrentRoles dep"
```

---

## Task 11: Remove old deps from `src/iris/auth/deps.py`

**Files:**
- Modify: `src/iris/auth/deps.py`

After Task 10, `CurrentUser`, `OptionalCurrentUser`, `CurrentSession`, `SessionData`, and their underlying `_current_*` / `_session_data` / `_required_session` helpers have no callers. Drop them. While here, rename `_resolve_session` → `_resolve_stored` and `_ResolvedSession` → `_StoredSession` to match the spec's vocabulary.

- [ ] **Step 1: Verify no remaining callers of the old surface names**

Run: `grep -rn -E "\b(CurrentUser|OptionalCurrentUser|CurrentSession|SessionData|_required_session|_RequiredSession|_current_user|_optional_current_user|_current_session|_session_data)\b" src/ tests/`
Expected: matches only inside `src/iris/auth/deps.py` (the definitions this task is about to remove). No matches in any test or other production file.

- [ ] **Step 2: Replace `src/iris/auth/deps.py` with the slim version**

```python
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Request

from iris.auth.authz.core import CurrentMapping, resolve_roles
from iris.auth.exceptions import AuthRequired
from iris.auth.identity import UserSession
from iris.auth.session import Session as _SessionT
from iris.auth.sessions import InMemorySessionStore


def set_session_store(app: FastAPI, store: InMemorySessionStore) -> None:
    app.state.auth_session_store = store


def set_settings(app: FastAPI, *, cookie_name: str, cookie_secure: bool = True) -> None:
    app.state.auth_cookie_name = cookie_name
    app.state.auth_cookie_secure = cookie_secure


def _get_store(request: Request) -> InMemorySessionStore:
    return request.app.state.auth_session_store


def _get_cookie_name(request: Request) -> str:
    return request.app.state.auth_cookie_name


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def _resolve_stored(request: Request) -> UserSession | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name) or _bearer(
        request.headers.get("authorization")
    )
    if not sid:
        return None
    store = _get_store(request)
    return await store.get_and_refresh(sid)


_StoredSession = Annotated[UserSession | None, Depends(_resolve_stored)]


async def _build_optional(
    stored: _StoredSession,
    mapping: CurrentMapping,
) -> _SessionT | None:
    if stored is None:
        return None
    return _SessionT(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        roles=resolve_roles(stored.user, mapping),
    )


_BuiltOptional = Annotated[_SessionT | None, Depends(_build_optional)]


async def _build_required(view: _BuiltOptional) -> _SessionT:
    if view is None:
        raise AuthRequired()
    return view


Session = Annotated[_SessionT, Depends(_build_required)]
OptionalSession = Annotated[_SessionT | None, Depends(_build_optional)]
```

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass. The deps pipeline now flows: `_resolve_stored` (raw store hit) → `_build_optional` (build Session view + roles) → `_build_required` (raise on None). Same caching behavior as before; cleaner names.

- [ ] **Step 4: Commit**

```bash
git add src/iris/auth/deps.py
git commit -m "refactor(auth): drop CurrentUser/CurrentSession/SessionData deps"
```

---

## Task 12: Update `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

The Authentication section documents an API that no longer exists. Rewrite the affected paragraphs.

- [ ] **Step 1: Update the "Authentication" intro block**

Replace the imports paragraph (currently around line 102 in CLAUDE.md, the line that starts ```from iris.auth import CurrentUser, OptionalCurrentUser, ...```) with:

```python
from iris.auth import Session, OptionalSession, require_role, User, install
```

And replace the descriptive paragraph that follows it ("`CurrentUser` requires a valid session...") with:

```
`Session` requires a valid session (cookie or `Authorization: Bearer <session-id>`); routes that take it 401 if no session is present. `OptionalSession` returns `None` when there's no session and never raises. Both are FastAPI dependency aliases — use them as parameter type annotations (`async def f(session: Session): ...`) and the dep system fills in a request-scoped `Session` view.

The `Session` view exposes everything routes legitimately need from a logged-in session: `id`, `user` (a `User`), `created_at`, `expires_at`, `data` (the per-session mutable dict), and `roles` (a `frozenset[str]` of effective role names with `includes:` closure already applied). The `data` field is the same dict object as the session store's storage, so `session.data[key] = value` writes through with no commit step. All other fields are frozen.

`require_role("admin")` is a dependency factory that 403s if the user's effective role set doesn't contain the named role. It returns a `Session`, so role-gated routes write `session: Session = Depends(require_role("admin"))` and access `session.user`/`session.data`/`session.roles` from the same value. See "Authorization (roles)" below for the YAML schema and inheritance semantics.
```

- [ ] **Step 2: Update the "Per-session server-side data" subsection**

Replace the example block (currently ```from iris.auth import SessionData, CurrentSession ...```) with:

```python
from iris.auth import Session

@app.post("/draft")
async def save_draft(session: Session, body: dict):
    session.data["draft"] = body         # direct mutation persists; no commit step
    return {"ok": True}

@app.get("/draft")
async def get_draft(session: Session):
    return session.data.get("draft", {})

@app.get("/me/full")
async def me_full(session: Session):
    return {
        "id": session.id,
        "logged_in_at": session.created_at,
        "data_keys": list(session.data),
        "roles": sorted(session.roles),
    }
```

Replace the bullet block underneath it with:

```
- `Session.data` is the dict directly — mutation writes through to the store.
- `Session` exposes `id`, `user`, `created_at`, `expires_at`, `data`, and `roles` on a single value. Routes that need only the user write `session.user`; routes that need the per-session bag write `session.data`.

A single `Session` dep injection resolves the underlying session lookup, the per-request `Session` view construction, and the role computation exactly once. A route taking both `session: Session` and `Depends(require_role(...))` makes one store hit, computes roles once, and runs the role check on the cached view.
```

- [ ] **Step 3: Update the "Authorization (roles)" example**

Replace:

```python
from iris.auth import require_role, CurrentRoles, CurrentUser

@app.get("/docs")
async def list_docs(user: User = Depends(require_role("reader"))):
    ...

@app.get("/me/roles")
async def my_roles(roles: CurrentRoles):
    return {"roles": sorted(roles)}
```

With:

```python
from iris.auth import Session, require_role

@app.get("/docs")
async def list_docs(session: Session = Depends(require_role("reader"))):
    ...

@app.get("/me/roles")
async def my_roles(session: Session):
    return {"roles": sorted(session.roles)}
```

And update the surrounding sentence about `CurrentRoles` to describe `session.roles` instead.

- [ ] **Step 4: Update the "Module map"**

In the module map block, replace the `__init__.py` line and add `session.py` and `authz/core.py`:

```
src/iris/auth/
├── __init__.py        # public surface: Session, OptionalSession, require_role, User, install
├── session.py         # Session frozen dataclass (request-scoped view)
├── config.py          # AuthSettings.from_env() + per-method sub-settings
├── identity.py        # User (frozen+slots), UserSession (mutable for sliding TTL; internal)
├── sessions.py        # InMemorySessionStore: create / get_and_refresh / delete
├── exceptions.py      # AuthRequired, AuthForbidden, AuthError, AuthorizationMisconfigured + install_exception_handlers
├── deps.py            # Session, OptionalSession, set_session_store, set_settings
├── csrf.py            # double-submit CSRF: mint_csrf_token, attach_csrf_cookie, issue_csrf_token, verify_csrf_form, delete_csrf_cookie
├── rate_limit.py      # TokenBucket: in-process per-key token-bucket limiter (used on POST /login)
├── routes.py          # /login, /login/callback, /logout, /api/whoami; install(app)
├── providers/
│   ├── __init__.py    # build_provider(settings) factory dispatching AUTH_METHOD
│   ├── base.py        # Provider Protocol
│   ├── mock.py        # MockProvider (config-driven creds, returns configured groups)
│   ├── ldap.py        # LDAPProvider (ldap3 bind + group search; tests use MOCK_SYNC)
│   └── oauth.py       # OAuthProvider (OIDC discovery + PKCE + signed-cookie state)
└── authz/
    ├── __init__.py    # (empty package marker)
    ├── config.py      # AuthzSettings.from_env() — reads AUTHZ_CONFIG_PATH
    ├── mapping.py     # RoleMapping value type + parse() with cycle detection + closure
    ├── loader.py      # RoleMappingLoader: mtime-cached, last-good fallback on bad reload
    ├── core.py        # resolve_roles, current_mapping helpers (no cross-package auth imports)
    └── deps.py        # require_role(name) factory
```

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -q`
Expected: all tests pass. (Docs updates don't change runtime behavior, but a sanity run never hurts.)

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(auth): document Session/OptionalSession in CLAUDE.md"
```

---

## Task 13: Final gates

**Files:** none (verification only)

Run the gates listed in CLAUDE.md. If any of them fails, fix in place and commit before declaring done.

- [ ] **Step 1: ruff**

Run: `uv run ruff check`
Expected: only the pre-existing `E402` in `src/iris/__init__.py` (the `from iris.app import app` after `load_dotenv()`). No new findings.

- [ ] **Step 2: basedpyright (errors)**

Run: `uv run basedpyright --level error`
Expected: `0 errors, 0 warnings, 0 notes`.

- [ ] **Step 3: basedpyright (warnings)**

Run: `uv run basedpyright --level warning`
Expected: `0 errors, 0 warnings, 0 notes`.

If a warning surfaces in `src/iris/auth/session.py`, `src/iris/auth/authz/core.py`, `src/iris/auth/deps.py`, or `src/iris/auth/authz/deps.py`, treat it as a real signal — investigate before suppressing.

- [ ] **Step 4: pytest**

Run: `uv run pytest`
Expected: all tests pass. Test count is roughly stable: 11 deleted from `test_deps.py`, 11 added in `test_session_dep.py` (the require_role-specific cases collapse into `test_authz_deps.py`'s existing coverage; two new role-closure cases compensate).

- [ ] **Step 5: Smoke-run the dev server (manual)**

Run: `uv run iris &` then `curl -s http://127.0.0.1:8000/login | head -20` to confirm the login page renders, then `kill %1`.
Expected: HTML page renders (200), no Python tracebacks in stderr.

(This catches things type-checking and unit tests don't — for example, a missing import that only surfaces at request time.)

- [ ] **Step 6: Commit any drift fixes**

If gates required fixes:

```bash
git add -A
git commit -m "chore(auth): fix gate-revealed drift"
```

If gates were already clean, no commit needed.

---

## Notes for the executing engineer

- **Frequent commits.** Each task ends with at least one commit. Between tasks, run `git log --oneline` to confirm the progression matches the plan.
- **Don't skip tests.** Every task that adds or changes behavior runs `pytest` (or a focused subset) before committing. The plan's "Run X, expected Y" lines aren't suggestions.
- **Don't pre-fetch.** Each task's "Files" header lists every file you'll touch. If you find yourself editing a file not listed, stop and reconsider — either the plan missed something (worth noting) or you've drifted from the task's scope.
- **Type-checker as a tripwire.** basedpyright is configured at zero-warning. If you introduce a `# type: ignore` to silence it, that's a smell — surface it instead of papering over it.
- **The `Session` class vs alias.** When in doubt: `Session` from `iris.auth` is the FastAPI dep alias (use as a parameter annotation). `Session` from `iris.auth.session` is the dataclass (use for `isinstance` checks, return-type annotations, or constructing a Session by hand in a test).
