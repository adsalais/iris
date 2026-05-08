# High-severity fixes + code documentation pass — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the eight High-severity defects from the code review and close the ten code-documentation gaps, without expanding into Medium/Low items.

**Architecture:** Seven small bug-fix commits (each independently green) + one atomic refactor commit at the end (handle.py elimination, Liskov fix, logger key normalization). Total: 8 commits.

**Tech Stack:** Python 3.13, FastAPI, ClickHouse via clickhouse-connect, SQLite for sessions, ldap3, PyJWT, itsdangerous, pytest + testcontainers.

**Reference spec:** `docs/superpowers/specs/2026-05-08-high-severity-fixes-design.md`

**Conventions:**
- All files paths are relative to `/home/driou/dev/project/iris`.
- Every code edit is followed by `uv run pytest -x`, `uv run ruff check`, and `uv run basedpyright --level error` until clean.
- Commit message style follows the recent log: `<type>(<scope>): <imperative subject>` (e.g., `fix(auth):`, `refactor(clickhouse):`).
- Tests use the existing fixtures: `client`, `authed_client` (in `tests/conftest.py`), `ch_client`, `prefix` (in `tests/clickhouse/conftest.py`).

---

## Task 1: Delete stale `# not implemented yet` comments (D1)

**Files:**
- Modify: `src/iris/auth/providers/__init__.py:13,18`

- [ ] **Step 1: Remove the two stale comments.**

In `src/iris/auth/providers/__init__.py`, change:
```python
        from iris.auth.providers.ldap import LDAPProvider  # not implemented yet (Task 10)
```
to:
```python
        from iris.auth.providers.ldap import LDAPProvider
```

And change:
```python
        from iris.auth.providers.oauth import OAuthProvider  # not implemented yet (Task 11)
```
to:
```python
        from iris.auth.providers.oauth import OAuthProvider
```

- [ ] **Step 2: Run the suite to confirm nothing broke.**

```bash
uv run pytest -x
```
Expected: all tests pass (the change is a comment-only edit).

- [ ] **Step 3: Run lint + typecheck.**

```bash
uv run ruff check
uv run basedpyright --level error
```
Expected: zero issues.

- [ ] **Step 4: Commit.**

```bash
git add src/iris/auth/providers/__init__.py
git commit -m "$(cat <<'EOF'
docs(auth): remove stale 'not implemented yet' comments

LDAPProvider and OAuthProvider are both fully implemented.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Shutdown-hook registry + SessionStore docstring (F1, D4)

**Files:**
- Modify: `src/iris/app.py:19-34` — `_lifespan` reads from registry instead of named attrs
- Modify: `src/iris/auth/routes.py:177-219` — `install` appends to registry
- Modify: `src/iris/clickhouse/install.py:31-91` — `install` appends to registry
- Modify: `src/iris/auth/sessions.py:83-103` — add `__init__` docstring (D4)
- Test: `tests/test_app.py` (append a new test)

- [ ] **Step 1: Write the failing test for LIFO shutdown order.**

Append to `tests/test_app.py`:
```python
def test_shutdown_hooks_run_in_lifo_order():
    """Hooks registered into app.state.shutdown_hooks fire in reverse-of-registration order."""
    from iris.app import build_app
    from fastapi.testclient import TestClient

    app = build_app(install_clickhouse=False)
    order: list[str] = []

    async def first():
        order.append("first")

    async def second():
        order.append("second")

    app.state.shutdown_hooks.append(first)
    app.state.shutdown_hooks.append(second)

    with TestClient(app):
        pass  # startup runs; exit triggers shutdown

    # Of the hooks we appended, second fires before first (LIFO).
    appended_order = [name for name in order if name in ("first", "second")]
    assert appended_order == ["second", "first"]


def test_build_app_initializes_shutdown_hooks_list():
    """build_app() exposes app.state.shutdown_hooks as a populated list."""
    from iris.app import build_app

    app = build_app(install_clickhouse=False)
    assert isinstance(app.state.shutdown_hooks, list)
    # auth.install registers at least the session-store closer.
    assert len(app.state.shutdown_hooks) >= 1
```

- [ ] **Step 2: Run to confirm both tests fail.**

```bash
uv run pytest tests/test_app.py::test_shutdown_hooks_run_in_lifo_order tests/test_app.py::test_build_app_initializes_shutdown_hooks_list -v
```
Expected: FAIL with `AttributeError: ... 'shutdown_hooks'` or similar.

- [ ] **Step 3: Modify `src/iris/app.py:19-34` lifespan to use a registry.**

Replace the `_lifespan` function in `src/iris/app.py` with:
```python
@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Subsystems register teardown callables in app.state.shutdown_hooks during
    # build_app(); this lifespan runs them in LIFO order on shutdown so a
    # subsystem's teardown sees its dependencies still alive.
    yield
    for hook in reversed(app.state.shutdown_hooks):
        await hook()
```

In the same file, in `build_app(...)` insert this line BEFORE `install_auth(app)`:
```python
    app.state.shutdown_hooks: list[Callable[[], Awaitable[None]]] = []
```

Add the imports at the top of the file:
```python
from collections.abc import Awaitable, Callable
```
(`AsyncGenerator` is already imported.)

- [ ] **Step 4: Modify `src/iris/auth/routes.py:install` to register on the new list.**

In `src/iris/auth/routes.py`, in the `install` function, replace:
```python
    app.state.auth_close_session_store = store.close
```
with:
```python
    app.state.shutdown_hooks.append(store.close)
```

And replace:
```python
    if isinstance(provider, OAuthProvider):
        app.state.auth_close_provider = provider.close
```
with:
```python
    if isinstance(provider, OAuthProvider):
        app.state.shutdown_hooks.append(provider.close)
```

- [ ] **Step 5: Modify `src/iris/clickhouse/install.py:install` similarly.**

In `src/iris/clickhouse/install.py`, replace:
```python
    async def _close_http() -> None:
        await http_client.aclose()

    app.state.clickhouse_close_http = _close_http
```
with:
```python
    async def _close_http() -> None:
        await http_client.aclose()

    app.state.shutdown_hooks.append(_close_http)
```

- [ ] **Step 6: Add the SessionStore `__init__` docstring (D4).**

In `src/iris/auth/sessions.py`, modify the `SessionStore.__init__` method (currently starting around line 83) to add a docstring. After the `def __init__(...)` signature, before the body:
```python
    def __init__(
        self,
        *,
        path: str,
        ttl_seconds: int,
        absolute_ttl_seconds: int,
        max_per_user: int = 10,
    ) -> None:
        """Open a SQLite-backed session store.

        Args:
            path: SQLite file path; ``":memory:"`` is supported for tests.
            ttl_seconds: sliding TTL refreshed on every ``get_and_refresh``.
            absolute_ttl_seconds: hard upper bound from ``created_at``;
                sessions past this expire even if recently refreshed.
            max_per_user: oldest sessions are pruned on ``create()`` once a
                subject exceeds this count.

        Concurrency: one ``sqlite3.Connection`` per process, serialized by
        ``self._lock`` (asyncio). Sync ``sqlite3`` calls run via
        ``asyncio.to_thread`` so the event loop stays unblocked. WAL mode
        plus ``synchronous=NORMAL`` make the file safe to share across
        multiple uvicorn workers.

        Lifecycle: ``close()`` is idempotent and required (registered into
        ``app.state.shutdown_hooks`` by ``iris.auth.routes.install``).
        """
```

- [ ] **Step 7: Run the new tests to confirm they pass.**

```bash
uv run pytest tests/test_app.py::test_shutdown_hooks_run_in_lifo_order tests/test_app.py::test_build_app_initializes_shutdown_hooks_list -v
```
Expected: PASS.

- [ ] **Step 8: Run the full suite + lint + typecheck.**

```bash
uv run pytest -x
uv run ruff check
uv run basedpyright --level error
```
Expected: zero failures, zero issues.

- [ ] **Step 9: Commit.**

```bash
git add src/iris/app.py src/iris/auth/routes.py src/iris/clickhouse/install.py src/iris/auth/sessions.py tests/test_app.py
git commit -m "$(cat <<'EOF'
refactor(app): shutdown hooks via registry instead of named state attrs

_lifespan iterates app.state.shutdown_hooks in LIFO order. Renaming a
hook attribute can no longer silently no-op shutdown — every install
appends its closer explicitly.

Also: docstring SessionStore.__init__ to cover concurrency and lifecycle.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Bump `policy_name` digest to 16 hex chars (F2, D2)

**Files:**
- Modify: `src/iris/clickhouse/identifiers.py:39-52` — change `[:8]` to `[:16]` and update docstring
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py` — update existing assertion + add a 64-bit digest test

- [ ] **Step 1: Update the existing test that pins the digest length.**

In `tests/clickhouse/test_clickhouse_identifiers.py`, change `test_policy_name_basic_shape`:
```python
def test_policy_name_basic_shape():
    name = policy_name("orders", "lines", "writer", "EU")
    # <db>_<table>_<role>_<slug>_<16charhash>
    assert name.startswith("orders_lines_writer_EU_")
    suffix = name.split("_")[-1]
    assert len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in suffix)
```

- [ ] **Step 2: Add a new test pinning the 64-bit digest (regression guard).**

Append to `tests/clickhouse/test_clickhouse_identifiers.py`:
```python
def test_policy_name_uses_64_bit_digest():
    """Digest is 16 hex chars (64 bits). 32-bit collisions silently dropped
    the second policy via CREATE ROW POLICY IF NOT EXISTS."""
    name = policy_name("db", "t", "r", "any-value")
    digest = name.rsplit("_", 1)[-1]
    assert len(digest) == 16
```

- [ ] **Step 3: Run both tests to confirm they fail.**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py::test_policy_name_basic_shape tests/clickhouse/test_clickhouse_identifiers.py::test_policy_name_uses_64_bit_digest -v
```
Expected: both FAIL with assertion errors on the digest length (`assert 8 == 16`).

- [ ] **Step 4: Apply the fix in `src/iris/clickhouse/identifiers.py`.**

Replace the `policy_name` function (lines 39-52) with:
```python
def policy_name(database: str, table: str, role: str, value: str) -> str:
    """Build a row-policy name: ``<database>_<table>_<role>_<slug>_<16charhash>``.

    ``database``, ``table``, ``role`` are validated as identifiers. ``value`` is
    treated as opaque — non-[a-zA-Z0-9_] characters collapse to '_' for the
    slug, and a 16-character SHA-256 hex digest of the raw value is appended
    so distinct values that happen to share a slug (``'EU/UK'`` vs ``'EU UK'``)
    get distinct names.

    The 16-char (64-bit) digest matters because ``add_row_policy`` issues
    ``CREATE ROW POLICY IF NOT EXISTS`` — a hash collision on the same
    ``(database, table, role)`` triple would silently drop the second
    policy. 64 bits puts the birthday bound around 4 billion entries.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(role, kind="role")
    slug = _SLUG_RE.sub("_", value).strip("_") or "v"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{database}_{table}_{role}_{slug}_{digest}"
```

- [ ] **Step 5: Run tests to confirm they pass.**

```bash
uv run pytest tests/clickhouse/test_clickhouse_identifiers.py -v
```
Expected: all green.

- [ ] **Step 6: Run the full suite + lint + typecheck.**

```bash
uv run pytest -x
uv run ruff check
uv run basedpyright --level error
```
Expected: zero failures, zero issues.

- [ ] **Step 7: Commit.**

```bash
git add src/iris/clickhouse/identifiers.py tests/clickhouse/test_clickhouse_identifiers.py
git commit -m "$(cat <<'EOF'
fix(clickhouse): widen policy_name digest 8 -> 16 hex chars

CREATE ROW POLICY IF NOT EXISTS silently dropped the second value on a
32-bit collision against the same (database, table, role) triple.
64-bit digest pushes the birthday bound out to ~4B entries.

Migration: deployments with legacy 8-char-suffixed policies retain those
intact. Re-running add_row_policy for an existing (db, table, role,
value) creates a new 16-char-suffixed policy alongside the legacy one;
both are functionally equivalent. Operators should drop legacy policies
after upgrade if cleanup matters.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Deterministic bootstrap-admin detection (F3, D3)

**Files:**
- Modify: `src/iris/clickhouse/bootstrap.py` — replace `_has_admin_role_with_suffix` with `_admin_already_bootstrapped`
- Modify: `tests/clickhouse/test_bootstrap_admin.py` — add false-positive regression test

- [ ] **Step 1: Write the failing test.**

Append to `tests/clickhouse/test_bootstrap_admin.py`:
```python
def test_bootstrap_user_channel_runs_when_unrelated_user_holds_role_admin(ch_client, prefix):
    """If an unrelated _USER role holds ROLE ADMIN WGO (e.g., manual operator
    grant), bootstrap should still seed the configured admin user. The old
    heuristic skipped here, leaving the configured admin un-bootstrapped."""
    _drop_admin_roles_with_suffix(ch_client, "_USER")

    # Pre-seed an unrelated _USER role that holds ROLE ADMIN WGO.
    decoy_role = f"{prefix}_decoy_USER"
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{decoy_role}`")
    ch_client.command(f"GRANT ROLE ADMIN ON *.* TO `{decoy_role}` WITH GRANT OPTION")

    # The configured admin is a different user.
    user = f"{prefix}_real_admin"
    bootstrap_admin(ch_client, admin_user=user)

    # The configured admin must have been seeded (with iris_global_admin grant).
    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": f"{user}_USER"},
    ).result_rows
    assert any(g[0] == GLOBAL_ADMIN_ROLE for g in granted), (
        f"configured admin {user}_USER was not bootstrapped despite decoy "
        f"holding ROLE ADMIN; granted = {granted}"
    )

    # Cleanup so subsequent tests aren't affected.
    ch_client.command(f"DROP ROLE IF EXISTS `{decoy_role}`")


def test_bootstrap_user_channel_is_idempotent_for_same_admin(ch_client, prefix):
    """Re-running bootstrap with the same admin name is a no-op (no error,
    no duplicate grants)."""
    _drop_admin_roles_with_suffix(ch_client, "_USER")
    user = f"{prefix}_idempotent_admin"
    bootstrap_admin(ch_client, admin_user=user)
    # Second call should not raise; should leave state identical.
    bootstrap_admin(ch_client, admin_user=user)

    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
          AND granted_role_name = {ga:String}
        """,
        parameters={"r": f"{user}_USER", "ga": GLOBAL_ADMIN_ROLE},
    ).result_rows
    # CH no-ops a re-grant; either 0 or 1 row is acceptable, but never duplicated.
    assert len(granted) <= 1
```

- [ ] **Step 2: Run to confirm the false-positive test fails.**

```bash
uv run pytest tests/clickhouse/test_bootstrap_admin.py::test_bootstrap_user_channel_runs_when_unrelated_user_holds_role_admin -v
```
Expected: FAIL — the configured admin's `_USER` role is not granted `iris_global_admin` because the heuristic skipped bootstrap.

- [ ] **Step 3: Replace the heuristic in `src/iris/clickhouse/bootstrap.py`.**

Replace the `_has_admin_role_with_suffix` function (lines 26-39) with:
```python
def _admin_already_bootstrapped(client: Client, *, expected_role: str) -> bool:
    """Return True iff ``expected_role`` already has ``iris_global_admin`` granted.

    This is the deterministic alternative to the previous heuristic
    (which scanned for *any* role with the configured suffix that held
    ROLE ADMIN — vulnerable to false-positives from manual operator
    grants on unrelated roles).
    """
    rows = client.query(
        """
        SELECT count() FROM system.role_grants
        WHERE role_name = {role:String}
          AND granted_role_name = {ga:String}
        """,
        parameters={"role": expected_role, "ga": GLOBAL_ADMIN_ROLE},
    ).result_rows
    return cast(int, rows[0][0]) > 0
```

Update the call sites in `bootstrap_admin` (currently lines 77 and 85):
```python
    if admin_user:
        expected = f"{admin_user}{USER_ROLE_SUFFIX}"
        if not _admin_already_bootstrapped(client, expected_role=expected):
            role_q = quote_identifier(expected, kind="role")
            client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
            _grant_full_admin(client, role_q=role_q)
            client.command(f"GRANT {global_admin_q} TO {role_q}")
            logger.info("bootstrap: seeded admin role for user=%s", admin_user)

    if admin_group:
        expected = f"{admin_group}{GROUP_ROLE_SUFFIX}"
        if not _admin_already_bootstrapped(client, expected_role=expected):
            role_q = quote_identifier(expected, kind="role")
            client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
            _grant_full_admin(client, role_q=role_q)
            client.command(f"GRANT {global_admin_q} TO {role_q}")
            logger.info("bootstrap: seeded admin role for group=%s", admin_group)
```

Update the module docstring at the top of `bootstrap.py` if it references the old heuristic. Current docstring already describes the high-level behavior correctly; verify and adjust if needed.

- [ ] **Step 4: Run the new tests + the existing bootstrap suite to confirm everything passes.**

```bash
uv run pytest tests/clickhouse/test_bootstrap_admin.py -v
```
Expected: all green, including the new tests.

- [ ] **Step 5: Run the full suite + lint + typecheck.**

```bash
uv run pytest -x
uv run ruff check
uv run basedpyright --level error
```
Expected: zero failures, zero issues.

- [ ] **Step 6: Commit.**

```bash
git add src/iris/clickhouse/bootstrap.py tests/clickhouse/test_bootstrap_admin.py
git commit -m "$(cat <<'EOF'
fix(clickhouse): tie bootstrap-admin detection to configured role name

_has_admin_role_with_suffix scanned for any role with the suffix that
held ROLE ADMIN WGO. A manual operator grant on an unrelated role made
bootstrap silently skip the configured admin. Now we check whether the
*expected* role itself already has iris_global_admin — per-name, immune
to manual grants on unrelated roles.

Behavior change: operators who relied on the old heuristic to suppress
bootstrap by manually pre-creating a different admin role will see the
configured CLICKHOUSE_ADMIN_USER bootstrapped on next start. Intended.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Clear OAuth state cookie on callback error + `_wants_html` comment (F4, D5)

**Files:**
- Modify: `src/iris/auth/routes.py:127-138` — clear cookie on AuthError branch
- Modify: `src/iris/auth/exceptions.py:32-34` — add comment to `_wants_html`
- Test: `tests/auth/test_provider_oauth.py`

- [ ] **Step 1: Inspect existing OAuth callback tests.**

```bash
grep -n "login_callback\|/login/callback" tests/auth/test_provider_oauth.py
```
Expected: identifies the existing callback tests (state mismatch, missing state cookie, etc.) so the new test fits the pattern.

- [ ] **Step 2: Write the failing test.**

Append to `tests/auth/test_provider_oauth.py`. The test pattern is "set up an OAuth provider, attach it to a FastAPI app, exercise `/login/callback` with a deliberately broken state cookie, assert response is a redirect AND has a `Set-Cookie` clearing `OAUTH_STATE_COOKIE`."

A minimal version (placement after the existing callback tests):
```python
def test_callback_error_clears_state_cookie(provider, settings):
    """A failed callback (bad state cookie / mismatched state / missing code)
    must delete OAUTH_STATE_COOKIE so a stale signed cookie can't replay."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from iris.auth.providers.oauth import OAUTH_STATE_COOKIE
    from iris.auth.routes import build_auth_router
    from iris.auth.sessions import SessionStore

    app = FastAPI()
    app.state.shutdown_hooks = []
    app.state.auth_cookie_secure = False
    store = SessionStore(
        path=":memory:", ttl_seconds=3600, absolute_ttl_seconds=86400, max_per_user=10
    )
    app.state.auth_session_store = store
    app.state.auth_cookie_name = "iris_session"
    app.state.post_login_hooks = []
    from iris.templates import TEMPLATES
    app.state.templates = TEMPLATES
    router = build_auth_router(
        app=app, provider=provider, store=store,
        cookie_name="iris_session", cookie_secure=False, ttl_seconds=3600,
    )
    app.include_router(router)
    from iris.auth.exceptions import install_exception_handlers
    install_exception_handlers(app, cookie_name="iris_session")

    client = TestClient(app)
    # No state cookie set -> AuthError("oauth_state") -> redirect to /login?error=...
    r = client.get("/login/callback", follow_redirects=False)
    assert r.status_code == 302
    set_cookie = r.headers.get("set-cookie", "")
    assert OAUTH_STATE_COOKIE in set_cookie, (
        f"expected Set-Cookie clearing {OAUTH_STATE_COOKIE}; got: {set_cookie!r}"
    )
    # delete_cookie sets Max-Age=0 (or expires in the past); confirm it's a clear, not a set.
    assert ("Max-Age=0" in set_cookie) or ("max-age=0" in set_cookie) or ("expires=" in set_cookie.lower())
```

- [ ] **Step 3: Run to confirm it fails.**

```bash
uv run pytest tests/auth/test_provider_oauth.py::test_callback_error_clears_state_cookie -v
```
Expected: FAIL — current code returns the redirect without clearing the cookie.

- [ ] **Step 4: Apply the fix in `src/iris/auth/routes.py`.**

Replace the `except AuthError` block in `login_callback` (around lines 133-134):
```python
        except AuthError as err:
            return RedirectResponse(f"/login?error={err.token}", status_code=302)
```
with:
```python
        except AuthError as err:
            response = RedirectResponse(f"/login?error={err.token}", status_code=302)
            response.delete_cookie(OAUTH_STATE_COOKIE)
            return response
```

- [ ] **Step 5: Add the `_wants_html` comment in `src/iris/auth/exceptions.py:32-34`.**

Replace:
```python
def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept
```
with:
```python
def _wants_html(request: Request) -> bool:
    # Treats `Accept: text/html` (browsers navigating) as HTML. Bare `Accept: */*`
    # (default for `fetch()` and tools like curl without -H) falls through to
    # the JSON branch — callers that want HTML must say so.
    accept = request.headers.get("accept", "")
    return "text/html" in accept
```

- [ ] **Step 6: Run the new test + full suite.**

```bash
uv run pytest tests/auth/test_provider_oauth.py::test_callback_error_clears_state_cookie -v
uv run pytest -x
uv run ruff check
uv run basedpyright --level error
```
Expected: green throughout.

- [ ] **Step 7: Commit.**

```bash
git add src/iris/auth/routes.py src/iris/auth/exceptions.py tests/auth/test_provider_oauth.py
git commit -m "$(cat <<'EOF'
fix(auth): clear OAUTH_STATE_COOKIE on /login/callback error

The success path already deletes the cookie; the AuthError branch did
not, leaving a signed state cookie alive for its 10-min TTL after a
failed callback.

Also: comment _wants_html to document the */* fallthrough.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Replace `assert` with explicit raise + `templates.py` docstring (F7, D8)

**Files:**
- Modify: `src/iris/auth/providers/__init__.py` — three `assert ... is not None` → explicit `raise RuntimeError`
- Modify: `src/iris/templates.py` — add module docstring
- Test: `tests/auth/test_config.py` (or new `tests/auth/test_provider_build.py` if naming clash; prefer the existing file)

- [ ] **Step 1: Write the failing test.**

Append to `tests/auth/test_config.py`:
```python
def test_build_provider_raises_runtime_error_when_method_mock_but_settings_missing():
    """If AuthSettings.method='mock' but settings.mock is None, build_provider
    must raise RuntimeError (not AssertionError, which `python -O` strips)."""
    from iris.auth.config import AuthSettings
    from iris.auth.providers import build_provider

    settings = AuthSettings(
        method="mock",
        cookie_name="iris_session",
        ttl_seconds=3600,
        absolute_ttl_seconds=86400,
        max_per_user=10,
        cookie_secure=False,
        auth_db_path=":memory:",
        oidc=None,
        ldap=None,
        mock=None,  # invariant violation
    )
    with pytest.raises(RuntimeError, match="mock"):
        build_provider(settings)


def test_build_provider_raises_runtime_error_when_method_ldap_but_settings_missing():
    from iris.auth.config import AuthSettings
    from iris.auth.providers import build_provider

    settings = AuthSettings(
        method="ldap",
        cookie_name="iris_session",
        ttl_seconds=3600,
        absolute_ttl_seconds=86400,
        max_per_user=10,
        cookie_secure=False,
        auth_db_path=":memory:",
        oidc=None,
        ldap=None,  # invariant violation
        mock=None,
    )
    with pytest.raises(RuntimeError, match="ldap"):
        build_provider(settings)


def test_build_provider_raises_runtime_error_when_method_oauth_but_settings_missing():
    from iris.auth.config import AuthSettings
    from iris.auth.providers import build_provider

    settings = AuthSettings(
        method="oauth",
        cookie_name="iris_session",
        ttl_seconds=3600,
        absolute_ttl_seconds=86400,
        max_per_user=10,
        cookie_secure=False,
        auth_db_path=":memory:",
        oidc=None,  # invariant violation
        ldap=None,
        mock=None,
    )
    with pytest.raises(RuntimeError, match="oauth"):
        build_provider(settings)
```

- [ ] **Step 2: Run to confirm they fail.**

```bash
uv run pytest tests/auth/test_config.py -k "build_provider_raises_runtime_error" -v
```
Expected: FAIL with `AssertionError` (since current code uses `assert`).

- [ ] **Step 3: Replace the asserts in `src/iris/auth/providers/__init__.py`.**

Replace the entire body of `build_provider` (lines 8-22) with:
```python
def build_provider(settings: AuthSettings) -> Provider:
    if settings.method == "mock":
        if settings.mock is None:
            raise RuntimeError(
                "AUTH_METHOD=mock requires settings.mock to be configured"
            )
        return MockProvider(settings.mock)
    if settings.method == "ldap":
        from iris.auth.providers.ldap import LDAPProvider

        if settings.ldap is None:
            raise RuntimeError(
                "AUTH_METHOD=ldap requires settings.ldap to be configured"
            )
        return LDAPProvider(settings.ldap)
    if settings.method == "oauth":
        from iris.auth.providers.oauth import OAuthProvider

        if settings.oidc is None:
            raise RuntimeError(
                "AUTH_METHOD=oauth requires settings.oidc to be configured"
            )
        return OAuthProvider(settings.oidc)
    raise ValueError(f"Unknown AUTH_METHOD: {settings.method}")
```

- [ ] **Step 4: Add the `templates.py` docstring (D8).**

Replace the contents of `src/iris/templates.py`:
```python
"""Shared `Jinja2Templates` instance for both root-level (`index.html`)
and auth-flow (`auth/*.html`) templates. Imported by `iris.app:build_app`
and re-exposed on `app.state.templates` so exception handlers and providers
can render without re-creating the loader.
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
```

- [ ] **Step 5: Run the new tests + full suite + lint + typecheck.**

```bash
uv run pytest tests/auth/test_config.py -v
uv run pytest -x
uv run ruff check
uv run basedpyright --level error
```
Expected: green throughout.

- [ ] **Step 6: Commit.**

```bash
git add src/iris/auth/providers/__init__.py src/iris/templates.py tests/auth/test_config.py
git commit -m "$(cat <<'EOF'
fix(auth): explicit raise instead of assert in build_provider

`assert settings.X is not None` is stripped under `python -O`; on a
malformed config in optimized mode, we'd None-deref later instead of
failing at startup. Now raises RuntimeError unconditionally.

Also: docstring iris.templates module.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Derive OAuth state-signing key + class docstrings (F8, D6, D7)

**Files:**
- Modify: `src/iris/auth/providers/oauth.py` — derive signing key, add class docstring
- Modify: `src/iris/auth/providers/ldap.py` — add class docstring
- Test: `tests/auth/test_provider_oauth.py`

- [ ] **Step 1: Write the failing test.**

Append to `tests/auth/test_provider_oauth.py`:
```python
def test_oauth_state_signing_key_is_not_the_client_secret(settings):
    """The state-signing key must not equal the OAuth client_secret. The signer
    is constructed from a SHA-256 derivation, so introspecting the signer's
    secret_key shows the derived bytes, not the raw secret."""
    import hashlib
    from iris.auth.providers.oauth import OAuthProvider

    provider = OAuthProvider(settings)
    expected_derived = hashlib.sha256(
        b"iris-oauth-state-signing-v1:" + settings.client_secret.encode()
    ).digest()

    # itsdangerous URLSafeTimedSerializer stores the key as `.secret_keys` (list of bytes-likes).
    keys = list(provider._signer.secret_keys)
    assert keys, "signer should have at least one secret key"
    # Our derived bytes should appear; the raw client_secret should not.
    assert expected_derived in keys
    assert settings.client_secret.encode() not in keys


def test_oauth_state_round_trips_with_derived_key(settings):
    """End-to-end: signing then loading a state payload still works after
    the derivation change."""
    from iris.auth.providers.oauth import OAuthProvider, STATE_COOKIE_TTL

    provider = OAuthProvider(settings)
    signed = provider._signer.dumps({"state": "x", "verifier": "y", "next": "/"})
    loaded = provider._signer.loads(signed, max_age=STATE_COOKIE_TTL)
    assert loaded == {"state": "x", "verifier": "y", "next": "/"}
```

- [ ] **Step 2: Run to confirm they fail.**

```bash
uv run pytest tests/auth/test_provider_oauth.py::test_oauth_state_signing_key_is_not_the_client_secret tests/auth/test_provider_oauth.py::test_oauth_state_round_trips_with_derived_key -v
```
Expected: the first test FAILS (raw secret currently used as key); the second test PASSES (round-trip works regardless).

- [ ] **Step 3: Apply the fix in `src/iris/auth/providers/oauth.py`.**

Add `import hashlib` near the top of the file (alongside `import base64`).

Replace the line that constructs the signer (currently around line 55):
```python
        self._signer = URLSafeTimedSerializer(settings.client_secret, salt="iris-oauth-state")
```
with:
```python
        # Derive the state-signing key from client_secret so a leak of one
        # is not a leak of the other. The "v1" tag in the prefix lets us
        # rotate the derivation later without invalidating in-flight cookies
        # mid-deploy. SHA-256 is one-way; raw client_secret stays out of the
        # signer.
        derived_key = hashlib.sha256(
            b"iris-oauth-state-signing-v1:" + settings.client_secret.encode()
        ).digest()
        self._signer = URLSafeTimedSerializer(derived_key, salt="iris-oauth-state")
```

- [ ] **Step 4: Add the `OAuthProvider` class docstring (D6).**

In `src/iris/auth/providers/oauth.py`, add a class docstring immediately after `class OAuthProvider:`:
```python
class OAuthProvider:
    """OIDC authorization-code-with-PKCE provider.

    Construction is lazy: discovery (`/.well-known/openid-configuration`) and
    JWKS fetch happen on the first property access (``authorize_endpoint``,
    ``token_endpoint``, etc.). This keeps `build_app()` non-blocking even
    if the IdP is briefly unreachable.

    Two httpx clients are held: a sync ``Client`` used only by
    ``_ensure_discovered`` (PyJWKClient bypasses httpx and uses urllib;
    we do discovery + JWKS via the sync client so test transports compose
    correctly), and an async ``AsyncClient`` for token exchange and
    userinfo. Both honor ``OIDC_CA_CERT_PATH`` for private CAs.

    State cookie signing: ``URLSafeTimedSerializer`` is keyed by a SHA-256
    derivation of ``client_secret`` (prefixed with ``iris-oauth-state-signing-v1:``)
    so a leak of the signing key is not a leak of the OAuth client_secret.
    The ``v1`` tag lets us rotate the derivation in a future release
    without invalidating in-flight state cookies mid-deploy.

    Limitation: JWKS is cached on first discovery; IdP key rotation
    requires an app restart. Acceptable for v1; revisit if rotation matters.
    """
```

- [ ] **Step 5: Add the `LDAPProvider` class docstring (D7).**

In `src/iris/auth/providers/ldap.py`, add a class docstring immediately after `class LDAPProvider:`:
```python
class LDAPProvider:
    """LDAP simple-bind authentication with group-membership lookup.

    Two-stage flow: (1) ``bind`` as the user's DN with their password —
    success implies authenticated; (2) ``search`` the configured
    ``group_base_dn`` for entries whose ``member`` attribute references
    the bound DN. The bind attempt drives both authentication and
    authorization (no separate service-account bind).

    The ``_USERNAME_RE`` whitelist (``[A-Za-z0-9._-]{1,64}``) defends
    ``bind_dn_template.format(username=...)``: characters outside the
    whitelist (commas, equals, parentheses, NULs) cannot reach the
    template substitution, so the resulting DN is structurally safe even
    though we don't parse-and-recompose. Group-search input is escaped
    via ``ldap3.utils.conv.escape_filter_chars``.

    The class carries a file-level pyright suppression because ldap3's
    ``Entry`` exposes attributes dynamically (``entry.cn.value`` et al)
    based on the search's ``attributes=`` argument; static typing can't
    track these.
    """
```

- [ ] **Step 6: Run the tests + full suite + lint + typecheck.**

```bash
uv run pytest tests/auth/test_provider_oauth.py -v
uv run pytest -x
uv run ruff check
uv run basedpyright --level error
```
Expected: green throughout.

- [ ] **Step 7: Commit.**

```bash
git add src/iris/auth/providers/oauth.py src/iris/auth/providers/ldap.py tests/auth/test_provider_oauth.py
git commit -m "$(cat <<'EOF'
fix(auth): derive OAuth state-signing key from client_secret

URLSafeTimedSerializer was keyed by the literal client_secret; a leak
of the state cookie signer's key was a leak of the OAuth client secret.
Now derived via SHA-256 with the prefix b'iris-oauth-state-signing-v1:'
so the two secrets are operationally distinct. The v1 tag lets us
rotate the derivation later without invalidating in-flight cookies.

Also: class docstrings for OAuthProvider and LDAPProvider.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Atomic refactor — eliminate `handle.py`, fix Liskov, normalize loggers (F5, F6, D9, D10, CLAUDE.md update)

This is the only task that lands as a single atomic commit (per CLAUDE.md "atomic refactor" pattern). Within the task there are no green checkpoints between steps — only the final checkpoint matters. Run the full suite at the end.

**Files (all in one commit):**
- Create: `src/iris/clickhouse/queries.py`
- Delete: `src/iris/clickhouse/handle.py`
- Modify: `src/iris/auth/identity.py` — direct imports, inline `asyncio.to_thread`, remove `query_as_user` from `AuthSession`, drop `# pyright: ignore` (D10)
- Modify: `tests/clickhouse/test_handle.py` — imports + symbol renames
- Modify: `tests/clickhouse/test_handle_integration.py` — imports + symbol renames
- Modify: `CLAUDE.md` — update the line referencing `iris.clickhouse.handle.*_impl`
- Modify (D9): `src/iris/auth/routes.py`, `src/iris/clickhouse/install.py`, `src/iris/clickhouse/bootstrap.py` — normalize `logger.info` key=value vocabulary

- [ ] **Step 1: Create `src/iris/clickhouse/queries.py`.**

```python
"""Async ClickHouse query helpers.

Two transport stories:

- ``query_as_user`` POSTs to CH's HTTP endpoint via ``httpx`` so we can
  prepend ``EXECUTE AS <user>`` to the body. clickhouse-connect would
  rewrite the body with ``FORMAT Native`` and break the impersonation.
- ``query_as_service`` runs over clickhouse-connect's ``Client`` (no
  impersonation), wrapped in ``asyncio.to_thread`` to stay off the
  event loop.

Session methods (in ``iris.auth.identity``) are the only callers.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.query import QueryResult

from iris.clickhouse.identifiers import quote_identifier


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
    """
    body = f"EXECUTE AS {quote_identifier(username, kind='username')} {sql}"
    params: dict[str, str] = {"default_format": "JSONEachRow"}
    if database:
        params["database"] = database
    if parameters:
        for k, v in parameters.items():
            params[f"param_{k}"] = str(v)
    response = await http_client.post("/", params=params, content=body)
    response.raise_for_status()
    text = response.text.strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line]


async def query_as_service(
    client: Client,
    *,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
    database: str | None = None,
) -> QueryResult:
    """Run ``sql`` as the service identity (no impersonation). When
    ``database`` is supplied, clickhouse-connect's ``database=`` kwarg
    sets the default schema for unqualified names."""
    kwargs: dict[str, Any] = {}
    if parameters:
        kwargs["parameters"] = dict(parameters)
    if database:
        kwargs["database"] = database
    return await asyncio.to_thread(client.query, sql, **kwargs)
```

- [ ] **Step 2: Rewrite `src/iris/auth/identity.py`.**

Replace the entire file contents with the structure below. Each Session method calls `asyncio.to_thread(<sync_fn>, …)` directly or `await query_as_user(...)`. Module-aliased imports (`audit`, `policies`, `grants`) avoid name collisions where method names match module-level function names.

```python
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from clickhouse_connect.driver.query import QueryResult

from iris.auth.session import EMPTY_RIGHTS, Rights
from iris.clickhouse import audit, grants, policies
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    drop_tier_roles,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
    tier_role_name,
)
from iris.clickhouse.identifiers import quote_identifier, validate_identifier
from iris.clickhouse.queries import query_as_user, query_as_service
from iris.clickhouse.users import init_user_rights


@dataclass(frozen=True, slots=True)
class User:
    subject: str
    username: str
    display_name: str
    groups: tuple[str, ...]


@dataclass(slots=True)
class UserSession:
    """Internal mutable session row from the SQLite store.

    Routes consume the request-scoped immutable :class:`AuthSession` view via
    the alias deps in ``iris.auth.deps``. ``UserSession`` is the row shape that
    sliding-TTL refresh operates on.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    rights: Rights = EMPTY_RIGHTS


@dataclass(frozen=True, slots=True)
class AuthSession:
    """Request-scoped view of a logged-in session.

    Built once per request by the auth dep. Routes receive an ``AuthSession``
    (or one of its subclasses: :class:`DatabaseSession`,
    :class:`DatabaseAdminSession`, :class:`DatabaseCreatorSession`,
    :class:`AdminSession`) via the ``Annotated`` alias deps in
    ``iris.auth.deps``.

    Frozen except for ``data``: the dict is a per-request snapshot deserialized
    from the SQLite session store. Mutations to the dict do NOT auto-persist —
    call ``await session.persist_data()`` to write the current ``data`` dict
    back to the store before returning.

    The ``client`` / ``http_client`` / ``settings`` / ``store`` fields are
    references injected by the dep resolver. They are not part of the
    persistent identity (``compare=False``, ``repr=False``) so two sessions
    with identical ``id``/``user``/``rights``/etc. compare equal regardless
    of which connections happen to be wired in.

    Note: ``AuthSession`` does not expose a ``query_as_user`` method. CH
    impersonation requires a target database; the database-scoped
    subclasses (``DatabaseSession`` and below) carry the per-database
    ``query_as_user``. Admins query as the service identity via
    ``AdminSession.query_as_service``.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    rights: Rights
    client: Any = field(repr=False, compare=False)
    http_client: Any = field(repr=False, compare=False)
    settings: Any = field(repr=False, compare=False)
    store: Any = field(repr=False, compare=False)

    async def persist_data(self) -> None:
        """Write the current ``data`` dict back to the session store.

        Routes that mutate ``session.data`` and want the change to survive the
        request call this before returning. Values must be JSON-encodable;
        anything else raises ``TypeError`` at write time.
        """
        await self.store.update_data(self.id, self.data)


@dataclass(frozen=True, slots=True)
class DatabaseSession(AuthSession):
    """Session bound to a specific database (the path/query parameter that
    drove the alias dep). ``query_as_user`` is auto-scoped to ``self.database``.
    To query a different database, use a fully-qualified table name and let
    CH enforce privileges.
    """
    database: str

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await query_as_user(
            self.http_client,
            username=self.user.username,
            sql=sql,
            parameters=parameters,
            database=self.database,
        )


@dataclass(frozen=True, slots=True)
class DatabaseAdminSession(DatabaseSession):
    """Per-database admin session. Adds tier-grant/revoke/lifecycle/audit
    methods scoped to ``self.database``."""

    async def grant_reader(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user, self.client,
            database=self.database, tier=TIER_DBREADER, username=username,
        )

    async def grant_writer(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user, self.client,
            database=self.database, tier=TIER_DBWRITER, username=username,
        )

    async def add_admin_user(self, username: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_user, self.client,
            database=self.database, tier=TIER_DBADMIN, username=username,
        )

    async def revoke_reader(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user, self.client,
            database=self.database, tier=TIER_DBREADER, username=username,
        )

    async def revoke_writer(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user, self.client,
            database=self.database, tier=TIER_DBWRITER, username=username,
        )

    async def remove_admin_user(self, username: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_user, self.client,
            database=self.database, tier=TIER_DBADMIN, username=username,
        )

    async def grant_reader_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group, self.client,
            database=self.database, tier=TIER_DBREADER, group=group,
        )

    async def grant_writer_to_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group, self.client,
            database=self.database, tier=TIER_DBWRITER, group=group,
        )

    async def add_admin_group(self, group: str) -> None:
        await asyncio.to_thread(
            grant_tier_to_group, self.client,
            database=self.database, tier=TIER_DBADMIN, group=group,
        )

    async def revoke_reader_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group, self.client,
            database=self.database, tier=TIER_DBREADER, group=group,
        )

    async def revoke_writer_from_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group, self.client,
            database=self.database, tier=TIER_DBWRITER, group=group,
        )

    async def remove_admin_group(self, group: str) -> None:
        await asyncio.to_thread(
            revoke_tier_from_group, self.client,
            database=self.database, tier=TIER_DBADMIN, group=group,
        )

    async def delete_database(self) -> None:
        db_q = quote_identifier(self.database, kind="database")
        database = self.database
        client = self.client

        def _sync() -> None:
            client.command(f"DROP DATABASE IF EXISTS {db_q}")
            drop_tier_roles(client, database=database)

        await asyncio.to_thread(_sync)

    async def list_admin_members(self) -> list[str]:
        admin_role = tier_role_name(self.database, TIER_DBADMIN)
        client = self.client

        def _sync() -> list[str]:
            rows = client.query(
                "SELECT role_name FROM system.role_grants "
                "WHERE granted_role_name = {r:String}",
                {"r": admin_role},
            )
            return [cast(str, row["role_name"]) for row in rows.named_results()]

        return await asyncio.to_thread(_sync)

    async def list_grants(self) -> list[dict[str, Any]]:
        client = self.client
        database = self.database

        def _sync() -> list[dict[str, Any]]:
            result = client.query(
                "SELECT * FROM system.grants WHERE database = {d:String}",
                parameters={"d": database},
            )
            return list(result.named_results())

        return await asyncio.to_thread(_sync)

    async def list_row_policies(self) -> list[dict[str, Any]]:
        client = self.client
        database = self.database

        def _sync() -> list[dict[str, Any]]:
            result = client.query(
                "SELECT * FROM system.row_policies WHERE database = {d:String}",
                parameters={"d": database},
            )
            return list(result.named_results())

        return await asyncio.to_thread(_sync)


@dataclass(frozen=True, slots=True)
class DatabaseCreatorSession(AuthSession):
    """Session that can create new databases. Returned by the
    ``SessionDatabaseCreator`` alias when ``rights.is_admin`` or
    ``rights.can_create_database``."""

    async def create_database(self, name: str) -> None:
        validate_identifier(name, kind="database")
        quoted = quote_identifier(name, kind="database")
        creator_username = self.user.username
        client = self.client

        def _sync() -> None:
            client.command(f"CREATE DATABASE IF NOT EXISTS {quoted}")
            create_tier_roles(client, database=name)
            grant_tier_to_user(
                client, database=name, tier=TIER_DBADMIN, username=creator_username,
            )

        await asyncio.to_thread(_sync)


@dataclass(frozen=True, slots=True)
class AdminSession(AuthSession):
    """Global-admin session. Adds service-identity queries plus audit and
    row-policy operations. For per-database operations, the route should use
    ``SessionDatabaseAdmin`` (which admits admins via the ``is_admin``
    superset and returns a :class:`DatabaseAdminSession` bound to the path's
    database)."""

    async def query_as_service(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> QueryResult:
        return await query_as_service(
            self.client, sql=sql, parameters=parameters, database=database,
        )

    async def reprovision_user(self, *, username: str, groups: list[str]) -> None:
        await asyncio.to_thread(
            init_user_rights, self.client,
            username=username, groups=groups, settings=self.settings,
        )

    async def grant_select_to_database(self, *, database: str, role: str) -> None:
        await asyncio.to_thread(
            grants.grant_select_to_database, self.client,
            database=database, role=role,
        )

    async def grant_insert_update_to_table(
        self, *, database: str, table: str, role: str
    ) -> None:
        await asyncio.to_thread(
            grants.grant_insert_update_to_table, self.client,
            database=database, table=table, role=role,
        )

    async def add_row_policy(
        self, *, database: str, table: str, column: str, role: str, value: str
    ) -> None:
        await asyncio.to_thread(
            policies.add_row_policy, self.client,
            database=database, table=table, column=column, role=role, value=value,
        )

    async def revoke_row_policy(
        self, *, database: str, table: str, role: str, value: str
    ) -> None:
        await asyncio.to_thread(
            policies.revoke_row_policy, self.client,
            database=database, table=table, role=role, value=value,
        )

    async def user_grants(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.user_grants, self.client, username=username)

    async def role_grants(self, *, role: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.role_grants, self.client, role=role)

    async def user_role_memberships(
        self, *, username: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.user_role_memberships, self.client, username=username)

    async def user_row_policies(self, *, username: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.user_row_policies, self.client, username=username)

    async def role_row_policies(self, *, role: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(audit.role_row_policies, self.client, role=role)

    async def table_row_policies(
        self, *, database: str, table: str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            audit.table_row_policies, self.client,
            database=database, table=table,
        )
```

- [ ] **Step 3: Delete `src/iris/clickhouse/handle.py`.**

```bash
rm src/iris/clickhouse/handle.py
```

- [ ] **Step 4: Update `tests/clickhouse/test_handle.py` imports and symbol references.**

In `tests/clickhouse/test_handle.py`:
- Replace `from iris.clickhouse.handle import (...)` with `from iris.clickhouse.queries import query_as_user, query_as_service`.
- Find-and-replace `query_as_user_impl` → `query_as_user` and `query_as_service_impl` → `query_as_service` throughout the file.

If the file's docstring or top-of-file comment mentions `iris.clickhouse.handle`, update to `iris.clickhouse.queries`.

- [ ] **Step 5: Update `tests/clickhouse/test_handle_integration.py` similarly.**

In `tests/clickhouse/test_handle_integration.py`:
- Replace `from iris.clickhouse.handle import query_as_service_impl, query_as_user_impl` with `from iris.clickhouse.queries import query_as_user, query_as_service`.
- Find-and-replace `query_as_user_impl` → `query_as_user` and `query_as_service_impl` → `query_as_service` throughout the file.

- [ ] **Step 6: Normalize logger key=value vocabulary (D9).**

Canonical keys: `subject=`, `username=`, `display_name=`, `groups=`, `remote_addr=`, `method=`, `reason=`, `session_id=`. Existing `user=` (where it carries display_name) becomes `display_name=`.

In `src/iris/auth/routes.py`:

Replace:
```python
        logger.info(
            "auth: login user=%s subject=%s method=%s groups=%s",
            user.display_name,
            user.subject,
            method,
            list(user.groups),
        )
```
with:
```python
        logger.info(
            "auth: login display_name=%s subject=%s method=%s groups=%s",
            user.display_name,
            user.subject,
            method,
            list(user.groups),
        )
```

Replace:
```python
            logger.info(
                "auth: login_rate_limited remote_addr=%s wait_seconds=%.1f",
                client_host,
                wait,
            )
```
(no change needed; already uses `remote_addr=`).

Replace:
```python
            logger.info(
                "auth: login_failed username=%s reason=%s remote_addr=%s",
                username,
                err.token,
                client_host,
            )
```
(no change needed).

Replace:
```python
        logger.info(
            "auth: logout user=%s subject=%s",
            session.user.display_name,
            session.user.subject,
        )
```
with:
```python
        logger.info(
            "auth: logout display_name=%s subject=%s",
            session.user.display_name,
            session.user.subject,
        )
```

In `src/iris/clickhouse/install.py`, the `_provision_on_login` log line uses `user=` for `user.username`. Change to `username=`:

Replace:
```python
        logger.info(
            (
                "clickhouse: provisioned user=%s groups=%s "
                "rights=admin:%s creator:%s reader:%d writer:%d db_admin:%d"
            ),
            user.username,
            list(user.groups),
            ...
        )
```
with:
```python
        logger.info(
            (
                "clickhouse: provisioned username=%s groups=%s "
                "rights=admin:%s creator:%s reader:%d writer:%d db_admin:%d"
            ),
            user.username,
            list(user.groups),
            ...
        )
```

In `src/iris/clickhouse/bootstrap.py`, the two `logger.info` lines use `user=%s` and `group=%s` — these are *configured* admin channel identifiers (matching the `CLICKHOUSE_ADMIN_USER` / `CLICKHOUSE_ADMIN_GROUP` env-var names), not session users. **Leave them as-is.** The D9 vocabulary applies to runtime per-request session logs; bootstrap logs configured operator-supplied identifiers and the env-var-aligned key names are the right thing to keep.

Verify there are no other `logger.info` calls with the old vocabulary:
```bash
grep -rn 'logger\.info.*user=\|logger\.info.*\<user_subject=' src/iris/
```
Expected: no matches that aren't already updated.

- [ ] **Step 7: Update CLAUDE.md.**

In `CLAUDE.md`, find the line under `## Conventions`:
```
- **Session methods use top-level imports of `iris.clickhouse.handle.*_impl`**: lazy method-body imports were a workaround for a now-removed cycle. Don't regress.
```
Replace with:
```
- **Session methods import directly from `iris.clickhouse.{audit,grants,policies,users,queries}` and call `asyncio.to_thread(<sync_fn>, ...)` inline**: the previous `iris.clickhouse.handle.*_impl` thunk layer was deleted; methods talk to the sync helpers (and `query_as_user` / `query_as_service` for the async-only paths) directly. Don't reintroduce the indirection.
```

Also in `CLAUDE.md`, in the **How** module map section under `src/iris/clickhouse/`, the structure no longer has `handle.py`. Update if necessary (the current map doesn't list individual files inside clickhouse/, so likely no change needed — verify by reading the current CLAUDE.md content).

- [ ] **Step 8: Run the full suite + lint + typecheck.**

```bash
uv run pytest -x
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: all green. The `# pyright: ignore[reportIncompatibleMethodOverride]` is gone (D10 done).

If the test suite fails: do NOT split the commit. Fix issues inline; the atomic-refactor pattern requires this to land as one commit per CLAUDE.md.

- [ ] **Step 9: Verify the acceptance signals.**

```bash
test ! -f src/iris/clickhouse/handle.py && echo OK_handle_deleted || echo FAIL_handle_still_exists
grep -n "reportIncompatibleMethodOverride" src/iris/auth/identity.py && echo FAIL_pyright_ignore_remains || echo OK_pyright_ignore_gone
grep -n "assert.*is not None" src/iris/auth/providers/__init__.py && echo FAIL_assert_remains || echo OK_no_assert
```
Expected output:
```
OK_handle_deleted
OK_pyright_ignore_gone
OK_no_assert
```

- [ ] **Step 10: Commit.**

```bash
git add -A src/iris/clickhouse/ src/iris/auth/identity.py tests/clickhouse/test_handle.py tests/clickhouse/test_handle_integration.py CLAUDE.md src/iris/auth/routes.py src/iris/clickhouse/install.py
# Verify only intended files are staged:
git status
git commit -m "$(cat <<'EOF'
refactor(clickhouse): drop *_impl thunk layer; fix Liskov in DatabaseSession

handle.py was 419 LOC of `async def X_impl: await asyncio.to_thread(X, …)`
thunks. Adding a CH op required touching three files; the layer added
nothing the call sites couldn't do directly. Now Session methods call
`asyncio.to_thread(<sync_fn>, …)` inline; the two non-trivial async
helpers (`query_as_user`, `query_as_service`) live in a new
`iris.clickhouse.queries` module without the `_impl` suffix.

AuthSession no longer carries `query_as_user(database=)`; CH
impersonation requires a target database, so the method now lives
solely on `DatabaseSession` (no parent override → no Liskov violation
→ no `# pyright: ignore[reportIncompatibleMethodOverride]`).

Also: standardize logger key=value vocabulary across auth/clickhouse
install logs (display_name=, username= replacing the freeform user=).

CLAUDE.md updated to reflect the new shape.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification

- [ ] **Run the full pipeline once more, end-to-end.**

```bash
uv run pytest
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero failures, zero warnings.

- [ ] **Confirm acceptance criteria from the spec.**

```bash
git log --oneline -10  # confirm 8 commits beyond the spec commit
test ! -f src/iris/clickhouse/handle.py && echo "handle.py: deleted"
grep -c "reportIncompatibleMethodOverride" src/iris/auth/identity.py  # expect 0
grep -c "assert .* is not None" src/iris/auth/providers/__init__.py  # expect 0
grep -c "shutdown_hooks" src/iris/app.py  # expect >=1
```
