# Auth module reshape — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

A full-codebase review surfaced ~17 fixes that the user grouped into three independent specs: (1) auth module reshape + naming, (2) security hardening, (3) SQL/identifier hygiene. This spec covers the **first** of those three; the other two are queued and explicitly out of scope here.

The auth package today has three structural problems that compound each other:

- `auth/identity.py` is 446 LOC and mixes the user-identity dataclasses (`User`, `UserSession`) with five frozen request-scoped session-view classes (`AuthSession`, `DatabaseSession`, `DatabaseAdminSession`, `DatabaseCreatorSession`, `AdminSession`) that carry ~50 ClickHouse-talking methods. The session views are not "identity" by any natural reading.
- The two filenames `auth/session.py` (60 LOC, `Rights` only) and `auth/sessions.py` (`SessionStore`, the SQLite-backed store) read as a typo. Their contents are unrelated to each other and to "session" as the route layer uses the word.
- `identity.py` imports `SessionStore` only as a type annotation, but the import order forces a `# pyright: reportImportCycles=false` at the top of the file plus a `TYPE_CHECKING` guard around the import — both purely defensive workarounds for the cycle introduced by the file boundary.

The `Rights` vocabulary is also overloaded: the project uses "rights" in CH GRANT prose, in role-name suffixes (`_DBREADER` etc.), in the `Rights` Python class, and in three function names (`init_user_rights`, `derive_rights`, `set_rights`). The class is one of those four meanings — the *frozen post-login authorization snapshot* — and is more naturally called `Capabilities`, leaving "rights" to refer to CH GRANTs in prose.

Three trivial cleanups ride along because they touch files in the change set: a dead `logger` declaration in `exceptions.py`, an `async` keyword on `verify_csrf_form` that does no `await`, and the misnamed `init_user_rights` function which actually provisions a CH user identity.

## Goal

Reshape `iris.auth` and the cross-cutting `Rights` vocabulary so the type triad (`User → StoredSession → AuthSession`) and the file boundaries (`identity.py / rights.py / store.py / views.py`) communicate their purpose by name, eliminate the import-cycle workarounds, and land the three trivial cleanups.

## Non-goals (deferred to other specs)

- Security hardening: TokenBucket eviction (S1), proxy-aware client IP (S2), CDN script SRI (S3), OAuth state cookie path (S4), CSRF on JSON requests (S5), `_safe_next` CRLF guard (S8), `_safe_next` info logging (U5).
- SQL/identifier hygiene: database-name suffix validation, `_FIXED_STRING_RE` deduplication (B5), `quote_string`/`_marshal_array_element` escape unification (B6), `delete_database` orphan-grant sweep (U4).
- Any other naming polish (`tier_role_name`, `iris_global_admin` casing, etc.).
- Any behavioral change. **This is a pure rename + move.** The three cleanups are surgical (delete one line, drop one keyword, rename one function) and do not change behavior.

## Atomicity

Per the project's `Refactor pattern: spec → plan → atomic commit`, this lands as one big-bang commit on a feature branch — no incremental compatibility shims, no `Rights = Capabilities` aliases. `--level error` and `--level warning` pyright gates plus the full test suite enforce that nothing breaks at merge time.

---

## 1. Final module layout

```
src/iris/auth/
  __init__.py        # re-exports updated to Capabilities/AuthSession/etc.
  config.py          # unchanged
  csrf.py            # one-line change: verify_csrf_form drops `async` (U7)
  deps.py            # imports updated; field name rights→capabilities
  exceptions.py      # one-line change: drop dead `logger = ...` (U6)
  identity.py        # NEW CONTENTS — only `User` and `StoredSession`
  rights.py          # was session.py — `Capabilities`, `EMPTY_CAPABILITIES`,
                     # `capabilities_to_dict`, `capabilities_from_dict`
  store.py           # was sessions.py — `SessionStore` (column → capabilities_json,
                     # method `set_rights` → `set_capabilities`)
  views.py           # NEW FILE — `AuthSession` + `DatabaseSession` family
  rate_limit.py      # unchanged
  routes.py          # imports updated; whoami reads .capabilities
  providers/
    base.py / mock.py / ldap.py / oauth.py / _form.py / __init__.py
                     # imports updated only

src/iris/clickhouse/
  capabilities.py    # was rights.py — `derive_capabilities(...)`
  users.py           # `init_user_rights` → `provision_user`
  install.py         # _provision_on_login uses provision_user, derive_capabilities,
                     # set_capabilities; log line wording updated
  __init__.py        # re-exports updated
  ...                # all other modules: imports updated to follow renames
```

The `# pyright: reportImportCycles=false` block + `TYPE_CHECKING` guard at the top of today's `identity.py` (lines 1-13, 22-28) is **deleted**, not relocated. `views.py` imports `SessionStore` from `auth.store` directly; `identity.py` no longer references `SessionStore`. The cycle is gone, not silenced.

## 2. Complete rename mapping

### Class / dataclass renames

| Before | After | Lives in |
|---|---|---|
| `Rights` | `Capabilities` | `auth/rights.py` |
| `UserSession` | `StoredSession` | `auth/identity.py` |
| `AuthSession` | unchanged | `auth/views.py` |
| `DatabaseSession` | unchanged | `auth/views.py` |
| `DatabaseAdminSession` | unchanged | `auth/views.py` |
| `DatabaseCreatorSession` | unchanged | `auth/views.py` |
| `AdminSession` | unchanged | `auth/views.py` |
| `User` | unchanged | `auth/identity.py` |
| `SessionStore` | unchanged | `auth/store.py` |

### Constant / function renames

| Before | After | Lives in |
|---|---|---|
| `EMPTY_RIGHTS` | `EMPTY_CAPABILITIES` | `auth/rights.py` |
| `rights_to_dict` | `capabilities_to_dict` | `auth/rights.py` |
| `rights_from_dict` | `capabilities_from_dict` | `auth/rights.py` |
| `derive_rights(...)` | `derive_capabilities(...)` | `clickhouse/capabilities.py` |
| `init_user_rights(...)` | `provision_user(...)` | `clickhouse/users.py` |
| `SessionStore.set_rights(...)` | `SessionStore.set_capabilities(...)` | `auth/store.py` |

### Field / attribute renames

| Before | After | Where |
|---|---|---|
| `<session>.rights` | `<session>.capabilities` | `AuthSession` and all subclasses; consumed by `/api/whoami`, dep-gating predicates (`session.rights.is_admin` → `session.capabilities.is_admin`), and the post-login provisioning hook |

### Module renames (git mv)

| Before | After |
|---|---|
| `src/iris/auth/session.py` | `src/iris/auth/rights.py` |
| `src/iris/auth/sessions.py` | `src/iris/auth/store.py` |
| `src/iris/clickhouse/rights.py` | `src/iris/clickhouse/capabilities.py` |
| (new) | `src/iris/auth/views.py` |

### Persistence-layer changes

| Before | After |
|---|---|
| SQLite column `rights_json` | `capabilities_json` |
| JSON keys inside that column (`is_admin`, `can_create_database`, `db_admin`, `db_writer`, `db_reader`) | unchanged |

### Public API (`auth/__init__.py` re-export list)

The `__all__` is rewritten to:

```python
__all__ = [
    "AdminSession", "AuthSession",
    "Capabilities", "EMPTY_CAPABILITIES",
    "DatabaseAdminSession", "DatabaseCreatorSession", "DatabaseSession",
    "Session", "SessionAdmin", "SessionDatabaseAdmin", "SessionDatabaseCreator",
    "SessionOptional", "SessionRead", "SessionWrite",
    "User",
    "install",
]
```

Drops `Rights`, `EMPTY_RIGHTS`. `StoredSession` is **not** re-exported — it is an internal store-row type; only `auth.store` and `auth.views` need it.

### Anti-list (NOT renamed in this spec)

- `auth/csrf.py` constants (`CSRF_COOKIE_NAME`, `CSRF_FORM_FIELD`).
- `auth/deps.py` Annotated aliases (`Session`, `SessionRead`, `SessionWrite`, `SessionAdmin`, `SessionDatabaseAdmin`, `SessionDatabaseCreator`, `SessionOptional`).
- `User` dataclass.
- `app.state.auth_session_store`.
- `iris_global_admin` role name on the CH side.
- `USER_ROLE_SUFFIX` / `GROUP_ROLE_SUFFIX` constants.
- Any rename or restructuring of files outside the change set (`config.py`, `rate_limit.py`, `providers/*`, `templates*`, `middleware.py`).

---

## 3. Migration & deployment

### SQLite (session store)

The schema rename `rights_json → capabilities_json` is shipped by editing `_SCHEMA` in `auth/store.py`. There is no in-code migration. Operators upgrading an existing instance must:

1. Stop iris.
2. Delete the SQLite file at `AUTH_DB_PATH` (default `./iris-auth.db`) plus its `.db-wal` and `.db-shm` sidecars.
3. Start iris.

All in-flight sessions are invalidated; users re-login. Rationale: (a) the project is `0.1.0`, (b) sessions have a 12 h sliding TTL anyway, (c) `.gitignore` already lists `iris-auth.db*`.

A "Migration: 0.1.x → next" note is added to `docs/operations.md` describing the deletion step.

### ClickHouse

Nothing to migrate. CH does not store anything keyed by Python class names. Tier role names, user role names, group role names, the `iris_global_admin` sentinel — all unchanged on the CH side. Existing CH state survives the rename intact.

### Test fixtures

The conftest CH testcontainer is session-scoped; nothing to migrate. The auth-store tests use `:memory:` SQLite which is fresh per-process; nothing to migrate. Test code itself does need string-level updates to references (covered in §4).

---

## 4. Touch-list

### New files

- `src/iris/auth/views.py` — `AuthSession`, `DatabaseSession`, `DatabaseAdminSession`, `DatabaseCreatorSession`, `AdminSession`. Lifted from today's `identity.py` lines 72-446. Imports `User`, `StoredSession` from `auth.identity`; imports `Capabilities` from `auth.rights`; imports `SessionStore` from `auth.store` directly (no `TYPE_CHECKING` needed — the cycle is broken by the file split).

### Renamed files (git mv to preserve history)

- `src/iris/auth/session.py` → `src/iris/auth/rights.py`
- `src/iris/auth/sessions.py` → `src/iris/auth/store.py`
- `src/iris/clickhouse/rights.py` → `src/iris/clickhouse/capabilities.py`

### Edited files

| File | Why |
|---|---|
| `src/iris/auth/__init__.py` | `__all__` rewrite; import paths updated |
| `src/iris/auth/identity.py` | Trimmed to `User` + `StoredSession`; the AuthSession family migrates out; pyright/TYPE_CHECKING workaround at the top of the file is deleted |
| `src/iris/auth/rights.py` | (post-rename) `Rights → Capabilities`, `EMPTY_RIGHTS → EMPTY_CAPABILITIES`, `rights_to_dict`/`rights_from_dict` renamed |
| `src/iris/auth/store.py` | (post-rename) imports `User`, `StoredSession` from `auth.identity`; imports `Capabilities` from `auth.rights`; SQL schema column renamed; `set_rights → set_capabilities`; `_row_to_session` reads `capabilities_json` |
| `src/iris/auth/views.py` | (new) per above |
| `src/iris/auth/csrf.py` | `verify_csrf_form` loses `async` (U7) |
| `src/iris/auth/deps.py` | `_to_auth_session` field-set `rights → capabilities`; imports updated |
| `src/iris/auth/exceptions.py` | Drop `logger = logging.getLogger("iris.auth")` (U6); imports updated if needed |
| `src/iris/auth/routes.py` | `/api/whoami` reads `session.capabilities` (was `session.rights`); imports updated |
| `src/iris/auth/providers/oauth.py` | Likely unchanged today — only imports `User` from `auth.identity`, which keeps that name. Plan verifies via grep before declaring no-op. |
| `src/iris/auth/providers/ldap.py` | Same — only imports `User`. Verify in plan. |
| `src/iris/auth/providers/mock.py` | Same — only imports `User`. Verify in plan. |
| `src/iris/clickhouse/__init__.py` | re-exports `derive_capabilities` (was `derive_rights`); `provision_user` (was `init_user_rights`); module path updates |
| `src/iris/clickhouse/capabilities.py` | (post-rename) function renamed; returns `Capabilities`; imports `Capabilities` from `iris.auth.rights` |
| `src/iris/clickhouse/users.py` | `init_user_rights → provision_user` |
| `src/iris/clickhouse/install.py` | `_provision_on_login` calls `provision_user` and `derive_capabilities`; `store.set_rights → store.set_capabilities`; log line wording updated (`rights=` → `capabilities=`) |
| `src/iris/app.py` | imports updated if any reference touches the renamed modules |
| `CLAUDE.md` | Update the "Module map" block, the `Rights`/`Session` terminology references, and any line that names a renamed module/symbol |
| `docs/auth.md` | Type names + module paths updated to reflect the new layout |
| `docs/clickhouse.md` | Update if it references `derive_rights` / `init_user_rights` by name |
| `docs/operations.md` | Add the `iris-auth.db*` deletion migration note |

### Test files (mechanical updates — find/replace + import path renames)

All `tests/` files that reference any renamed symbol — by import or by string in an assertion / log scrape. Likely targets (the implementation plan will produce the exact list via grep before edits start):

- `tests/auth/test_rights.py` — likely renames internally (and possibly the file: `test_rights.py → test_capabilities.py`)
- `tests/auth/test_session_store*.py` — `UserSession → StoredSession`; `set_rights → set_capabilities`; column name in any direct SQL assertions
- `tests/auth/test_session_dep.py` — `session.rights → session.capabilities`
- `tests/auth/test_post_login_hook.py` — likely references `init_user_rights` or `derive_rights`
- `tests/clickhouse/test_rights_derivation.py` — likely renames to `test_capabilities_derivation.py`; imports `derive_capabilities`
- `tests/clickhouse/test_login_provisioning.py` — `init_user_rights → provision_user`
- `tests/clickhouse/conftest.py` — if it monkeypatches or references either symbol
- Any other `tests/clickhouse/test_*.py` that imports from `iris.clickhouse.rights`
- `tests/conftest.py` — if it touches any renamed symbol
- `tests/auth/integration/conftest.py` and related integration tests — same

---

## 5. Testing strategy

This is a pure refactor — no new tests, no new behavior.

**Pass criteria (all must be green on the feature branch before merge):**

1. `uv run pytest` — full suite, including `tests/auth/integration/` (Keycloak) and `tests/clickhouse/integration/` (Keycloak + CH). Integration suites are the strongest signal that the rename has not broken end-to-end behavior.
2. `uv run ruff check` — zero warnings.
3. `uv run basedpyright --level error` — zero errors.
4. `uv run basedpyright --level warning` — zero warnings (per CLAUDE.md, this is the merge gate, not just `--level error`).
5. Manual smoke test of the dev server: `uv run iris`, log in via the mock provider, confirm `/api/whoami` returns the new `capabilities` keys and `/` renders.

**No coverage regression.** Test count must stay ≥ pre-refactor; any test that was renamed (file or symbol) must still exist and still execute the same assertions. The plan should diff `pytest --collect-only` before/after and reconcile any test that disappeared.

**Confidence in renames.** `basedpyright --level error` catches every bare reference to a missing symbol, so any forgotten `Rights` / `UserSession` / `init_user_rights` / `set_rights` / `rights_json` / `derive_rights` reference fails the type-check gate. Combined with the test suite, that is enough; no custom check script.

**Pyright config change.** The file-level pyright suppression at the top of today's `identity.py` (`# pyright: reportImportCycles=false`) is deleted because the cycle is gone. If pyright reports new findings in `views.py` or `identity.py`, they are real bugs and must be fixed before merge — not re-suppressed.

---

## 6. Cleanup riders (bundled into the same commit)

Two trivial changes share the diff because they touch files we are already editing:

- **U6** — `src/iris/auth/exceptions.py:8`: delete the unused `logger = logging.getLogger("iris.auth")` line. No behavior change.
- **U7** — `src/iris/auth/csrf.py`: change `async def verify_csrf_form(...)` to `def verify_csrf_form(...)`. FastAPI dispatches sync deps on its threadpool; the function does no `await` today. Caveat: the plan must confirm no test calls `await verify_csrf_form(...)` directly; if any does, drop the `await` there too.

---

## 7. Out of scope (explicit, to prevent scope creep)

The following were called out in the review but are reserved for the **other two specs** (security hardening; SQL/identifier hygiene) and **must not** be touched in this commit:

- TokenBucket eviction (S1)
- Proxy-aware client IP (S2)
- CDN script SRI hash (S3)
- OAuth state cookie path (S4)
- CSRF on JSON requests (S5)
- `_safe_next` CRLF guard (S8)
- `_safe_next` info logging (U5)
- Database-name suffix validation (the `_DBADMIN`/`_USER` collision)
- `_FIXED_STRING_RE` deduplication (B5)
- `quote_string` vs `_marshal_array_element` escape unification (B6)
- `delete_database` orphan-grant sweep (U4)
- Any other naming polish (`tier_role_name`, `iris_global_admin` casing, etc.)
- Any consolidation of `DatabaseAdminSession`'s 12 grant/revoke methods.

If anyone is tempted while doing the rename, the plan instructs: leave it. A second pass (the security or SQL-hygiene spec) will pick those up with their own review window.
