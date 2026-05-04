# Session API Simplification — Design

**Status:** approved
**Date:** 2026-05-04
**Author:** Adrien Salais (with Claude)

## Goal

Collapse the `iris.auth` public surface from five overlapping deps + two value types down to two deps + two value types, organized around a single request-scoped `Session` view.

## Motivation

Today the auth package exposes five deps that each project a slightly different slice of the same underlying session:

```python
CurrentUser            # User; 401 if no session
OptionalCurrentUser    # User | None
CurrentSession         # UserSession (id, created_at, expires_at, user, data)
SessionData            # the per-session mutable dict
CurrentRoles           # frozenset[str] of effective role names
```

Plus `User` and `UserSession` as exported value types. A route that needs the user *and* their roles *and* their per-session bag has to either request three deps or request `CurrentSession` and remember to read `.user.username` / `.data["draft"]` / call out separately for roles. The mental overhead is real even at this codebase's scale.

The new shape: one `Session` view object exposes everything a route legitimately needs. Routes that need a logged-in user write `session: Session`. Routes that may run anonymous write `session: OptionalSession`. Roles-guarded routes still use `require_role(name)` and now also receive a `Session`.

## Public surface (after refactor)

```python
from iris.auth import Session, OptionalSession, require_role, User, install
```

| Name | Type | Purpose |
|------|------|---------|
| `Session` | FastAPI dep — `Annotated[SessionT, Depends(...)]` | Required-auth dep. 401 (`AuthRequired`) if no session. |
| `OptionalSession` | FastAPI dep — `Annotated[SessionT \| None, Depends(...)]` | Returns `None` when there's no session. Never raises. |
| `require_role(name)` | Dep factory | Returns a `Session`-yielding dep. 403 on role mismatch, 500 on misconfigured role (unchanged behavior from today). |
| `User` | Frozen dataclass | Public value type, used for `session.user: User` annotations and direct typing. |
| `install(app)` | Function | Boot wiring (unchanged). |

**Removed from the public surface, hard-cut, no aliases:**

- `CurrentUser` → use `Session.user`
- `OptionalCurrentUser` → use `OptionalSession` (then `.user` if you need it)
- `CurrentSession` → use `Session`
- `SessionData` → use `Session.data`
- `CurrentRoles` → use `Session.roles`
- `UserSession` → becomes internal storage type, no longer exported

## The `Session` value type

New module `src/iris/auth/session.py`:

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
    `Session` or `OptionalSession` annotated aliases.

    Frozen except for `data`, which is the SAME dict instance as the
    stored UserSession.data — so `session.data[key] = value` writes
    through to the session store with no commit step.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    roles: frozenset[str]
```

### Field semantics

- **`id`** — opaque session id (cookie value or bearer token).
- **`user`** — `User` (frozen): `subject`, `username`, `display_name`, `groups`.
- **`created_at`** / **`expires_at`** — sliding-TTL bounds. The hard absolute cap (`absolute_expires_at`) is intentionally **not** exposed: it's a store-internal concern, no route should reason about it.
- **`data`** — the SAME `dict` object as `UserSession.data` in the store. Mutations persist. This is the only "shared mutable" in an otherwise frozen value; documented in the docstring.
- **`roles`** — `frozenset[str]` of effective role names (computed via the existing `_resolve_roles(user, mapping)` helper, including `includes:` closure). Always populated, even on routes that don't read it. Cost is a few set unions; negligible at ≤20-user scale.

### Why frozen + shared `data`

Frozen fields prevent routes from accidentally mutating session metadata (`created_at`, `expires_at`, `user`, `roles`, `id`). The `data` dict is the one place per-session mutation is legitimately required, and the shared-reference trick keeps today's read-modify-write ergonomics working unchanged.

## The `UserSession` storage type — internal

Unchanged in `src/iris/auth/identity.py`:

```python
@dataclass(slots=True)
class UserSession:
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
```

Removed from `iris.auth.__all__`. The session store (`InMemorySessionStore`) keeps producing and consuming `UserSession`. Only the deps adapt it into a `Session` view per request. `absolute_expires_at` and the mutable shape stay where they belong: in the store.

## Deps wiring

The internal pipeline keeps its two-stage shape; only the names and the final shape change.

### Module reshuffle to avoid an import cycle

The naive wiring would have `iris.auth.deps` import `_resolve_roles` / `_current_mapping` from `iris.auth.authz.deps`, while `iris.auth.authz.deps` imports `Session` from `iris.auth.deps` — a cycle. Fix: split the authz dep module into two files.

| File | Contents | Auth imports |
|------|----------|--------------|
| `iris/auth/authz/core.py` (NEW) | `_resolve_roles(user, mapping)`, `_current_mapping(request)`, `_CurrentMapping` Annotated alias | only `iris.auth.identity.User`, `iris.auth.authz.mapping.RoleMapping` — no `iris.auth.deps` |
| `iris/auth/authz/deps.py` (slimmed) | `require_role(name)` factory | imports `Session` from `iris.auth.deps`, `_CurrentMapping` from `.core` |
| `iris/auth/deps.py` | `_resolve_stored`, `_build_optional`, `_build_required`, `Session`, `OptionalSession`, `set_session_store`, `set_settings` | imports `_resolve_roles` and `_CurrentMapping` from `iris.auth.authz.core` |

Edges: `deps → authz.core`, `authz.deps → deps`, `authz.deps → authz.core`. No cycle.

### Wiring

```python
# src/iris/auth/deps.py (after)

from iris.auth.session import Session as _SessionT
from iris.auth.identity import UserSession
from iris.auth.authz.core import _CurrentMapping, _resolve_roles


async def _resolve_stored(request: Request) -> UserSession | None:
    """Resolve the stored session by cookie or bearer. None if missing/expired."""
    # body unchanged from today's _resolve_session


_StoredSession = Annotated[UserSession | None, Depends(_resolve_stored)]


async def _build_optional(
    stored: _StoredSession,
    mapping: _CurrentMapping,
) -> _SessionT | None:
    if stored is None:
        return None
    return _SessionT(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,                     # SAME dict instance
        roles=_resolve_roles(stored.user, mapping),
    )


async def _build_required(view: Annotated[_SessionT | None, Depends(_build_optional)]) -> _SessionT:
    if view is None:
        raise AuthRequired()
    return view


Session         = Annotated[_SessionT, Depends(_build_required)]
OptionalSession = Annotated[_SessionT | None, Depends(_build_optional)]
```

### Naming collision

The class is `Session` in `iris.auth.session`. The Annotated alias is also named `Session` in `iris.auth.deps`. To avoid a self-shadowing import inside `deps.py`, the class is imported under `_SessionT`. `iris.auth.__init__` re-exports only the Annotated alias. Callers writing `def f(session: Session)` always get the Annotated alias (which evaluates to the underlying class for type-checker purposes — `Annotated[X, ...]` is `X` for typing). The handful of internal helpers that need the *class* name can `from iris.auth.session import Session` directly.

### Dependency caching

A route taking both `session: Session` and `Depends(require_role("…"))` resolves `_resolve_stored` once via FastAPI's per-request dep cache, builds the `Session` view once, and feeds it to both the route signature and the role check. No double store hit, no double role computation. Same caching semantics as today's pipeline.

## `require_role` returns a Session

```python
# src/iris/auth/authz/deps.py (after)

from iris.auth.deps import Session
from iris.auth.session import Session as _SessionT


def require_role(role: str):
    async def _check(session: Session, mapping: _CurrentMapping) -> _SessionT:
        if role not in mapping.roles:
            raise AuthorizationMisconfigured(role)
        if role not in session.roles:
            raise AuthForbidden(
                needed=(role,),
                have=tuple(sorted(session.roles)),
            )
        return session
    return _check
```

`_check` depends on the public `Session` alias, so role-checked routes share the per-request resolution. The signature change from returning `User` to returning `Session` is the only behavioral diff; 403 / 500 behavior is unchanged.

`CurrentRoles` and the standalone `_current_roles` dep are removed. The `_resolve_roles(user, mapping)` helper and `_current_mapping` dep stay — relocated to `iris.auth.authz.core` (see "Module reshuffle" above).

### Usage at the route boundary

The user's preferred call site is:

```python
@app.get("/docs/list")
async def list_docs(session: Session = Depends(require_role("reader"))): ...
```

This works because FastAPI's parameter-resolution rule prioritizes an explicit `= Depends(...)` over the `Depends(...)` baked into the parameter's `Annotated` type. So the alias `Session = Annotated[_SessionT, Depends(_build_required)]` provides the *type* for static checking (the route receives a `_SessionT` instance), while the explicit `Depends(require_role("reader"))` provides the *value* via `_check`. Inside `_check`, the `session: Session` parameter resolves through the alias's `_build_required` dep — no override there — so the chain runs once, cached, with the role check layered on top.

Routes that don't need a role check use the bare alias:

```python
@app.get("/me")
async def me(session: Session): ...

@app.get("/")
async def home(session: OptionalSession): ...   # if anonymous access is allowed
```

## Migration impact

### Production code

- **`src/iris/app.py`** — 3 routes:
  - `index(request, user: CurrentUser)` → `index(request, session: Session)`. Use `session.user` where the template needs it.
  - `greet(signals, user: CurrentUser)` → `greet(signals, session: Session)`.
  - `clock(_user: CurrentUser)` → `clock(_session: Session)`.
- **`src/iris/auth/routes.py`** — `whoami(user: CurrentUser)` → `whoami(session: Session)`. Body uses `session.user`.

### Tests

- **`tests/auth/test_deps.py`** — rewrite all in-test route signatures to use `Session` / `OptionalSession`. Drop tests of removed names; their behavioral coverage migrates into the new test_session_dep file.
- **`tests/auth/authz/test_authz_deps.py`** — `require_role` returns a `Session`; assertions that read `User` migrate to `Session.user`. Tests of `CurrentRoles` migrate to `Session.roles`.
- **`tests/auth/test_error_pages.py`** — `Depends(require_role("admin"))` annotation type changes from `User` to `Session`.
- **NEW: `tests/auth/test_session_dep.py`** — covers:
  - `Session` dep returns a frozen view with all six fields populated.
  - `OptionalSession` returns `None` when no cookie / no bearer.
  - `Session.data` mutations write through to the session store across requests.
  - `Session.roles` reflects the YAML mapping (groups + users + includes closure).
  - `Session` raises `AuthRequired` (401 via the exception handler) when no session cookie.
- **`tests/conftest.py`** + `authed_client` fixture — already imports `User` from `iris.auth.identity` directly; that path keeps working. No changes.
- **`tests/test_app.py`** — does not reference deps directly. Unaffected.

### CLAUDE.md

Update the "Authentication" section to reflect the new public surface, replace the `CurrentUser`/`OptionalCurrentUser`/`CurrentSession`/`SessionData`/`CurrentRoles` paragraphs with `Session`/`OptionalSession` examples, and update the module map (`__init__.py` exports list, removed `CurrentRoles`/`SessionData`/etc.).

## Rollout

Single PR, hard-cut. Order of changes inside the PR:

1. Add `src/iris/auth/session.py` with the `Session` dataclass.
2. Add `src/iris/auth/authz/core.py`. Move `_resolve_roles`, `_current_mapping`, `_CurrentMapping` from `authz/deps.py` into it (no behavior change).
3. Slim `src/iris/auth/authz/deps.py` to just `require_role`. It now imports `Session` from `iris.auth.deps` and `_CurrentMapping` from `.core`. `require_role` returns Session; `CurrentRoles` and `_current_roles` removed.
4. Rewrite `src/iris/auth/deps.py` to expose only `Session` and `OptionalSession`. `_resolve_stored`, `set_session_store`, `set_settings` retained. Imports `_resolve_roles`/`_CurrentMapping` from `iris.auth.authz.core`.
5. Update `src/iris/auth/__init__.py` `__all__` to the new five names (`Session`, `OptionalSession`, `require_role`, `User`, `install`).
6. Update `src/iris/auth/routes.py` (whoami) and `src/iris/app.py` (3 routes).
7. Add `tests/auth/test_session_dep.py`. Update existing test files.
8. Update CLAUDE.md auth section + module map (add `session.py`, `authz/core.py`; remove `CurrentRoles`, `SessionData`, `CurrentSession`, `CurrentUser`, `OptionalCurrentUser` from the public surface line; replace with `Session, OptionalSession`).
9. Gates: `uv run ruff check` (one expected E402 in `src/iris/__init__.py`, no new findings), `uv run basedpyright --level error` (zero), `uv run basedpyright --level warning` (zero), `uv run pytest` (all green).

## Non-goals

- No changes to the session store, cookie handling, OAuth/LDAP/Mock providers, CSRF, rate limiting, exception handlers, or the role-mapping YAML schema.
- No deprecation cycle. Hard-cut, callers updated in the same PR.
- No new public types beyond `Session`. `User` remains; `UserSession` is removed from public surface but unchanged internally.

## Risks & mitigations

- **Naming collision (`Session` class vs `Session` annotated alias) confuses readers.** Mitigation: the class lives in its own module (`iris.auth.session`); the alias is the *only* `Session` exported from `iris.auth`. Inside `deps.py` the class is imported as `_SessionT`. The convention is documented in the `Session` class docstring and in CLAUDE.md.
- **Eager role resolution on every request, including for routes that don't read roles.** Acceptable: a few set unions per request, sub-microsecond at this scale. If profiling later shows otherwise, `roles` could be moved to a lazy `cached_property` — but that would re-introduce a "sometimes-computed" smell, so the current eager design is preferred.
- **`Session.data` shared-reference is a foot-gun for unsuspecting readers.** Mitigation: prominent docstring on `Session`. The semantics match today's `SessionData` dep, so the foot-gun isn't new — it's just centralized.

## Approval

Approved 2026-05-04 by Adrien Salais (decisions: A / B / A on the three forks).
