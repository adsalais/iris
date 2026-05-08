# High-severity fixes + code documentation pass

**Date:** 2026-05-08
**Status:** design

## Context

A code review of the iris codebase (~3361 LOC) catalogued issues across seven
categories: brittle behavior, code smells, unsound code, file organization,
entity naming, code documentation, and security. This spec covers the subset
the user accepted: every High-severity functional issue plus every code
documentation issue. Medium and Low items, file organization, and entity
naming are explicitly out of scope and may land in separate specs later.

## Goal

Eliminate the eight High-severity defects and the documentation gaps without
expanding scope into the M/L catalog. Each functional fix has a focused test
that would have caught the original defect.

## Functional fixes

### F1 — Shutdown-hook registry in `app.py`

**Defect:** `app.py:_lifespan` reads three close hooks from `app.state` by
string name via `getattr(..., None)`. Renaming a hook silently no-ops the
shutdown step; no test fails.

**Change:**

- Add `app.state.shutdown_hooks: list[Callable[[], Awaitable[None]]]` initialized
  to `[]` at the start of `_lifespan` (before `yield`).
- After `yield`, iterate `reversed(app.state.shutdown_hooks)` and `await` each.
  LIFO order matches setup-vs-teardown convention.
- Update `auth/routes.py:install` and `clickhouse/install.py:install` to
  `app.state.shutdown_hooks.append(...)` instead of writing
  `app.state.auth_close_*` / `app.state.clickhouse_close_*` attributes.
- Delete the now-unused state attribute names.

**Test (new, in `tests/test_app.py` or new `tests/test_lifespan.py`):**
construct an app, register two recorded hooks, exit lifespan, assert both ran
in LIFO order.

### F2 — `policy_name` digest 8 → 16 hex chars

**Defect:** `clickhouse/identifiers.py:policy_name` uses an 8-char SHA-256
hex digest (32 bits). Combined with `add_row_policy`'s `CREATE ROW POLICY IF
NOT EXISTS`, two distinct values colliding to the same name on the same
`(database, table, role)` triple would silently skip the second policy.

**Change:**

- Change `hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]` to `[:16]`.
- Update the docstring to mention the 16-char digest, the collision space
  (64 bits), and the `IF NOT EXISTS` interaction in `add_row_policy` (which is
  why a low-collision digest matters).

**Test (new):** in `tests/clickhouse/test_clickhouse_identifiers.py`, assert
`len(digest_part)` is 16 in the produced policy name.

**Migration note for commit message:** old deployed CH instances retain their
8-char-suffixed legacy policies. New code computes 16-char names; on
re-`add_row_policy` for an existing `(db, table, role, value)`, a second
policy with the new name would be created alongside the legacy one. Operators
should drop legacy policies after upgrade if cleanup matters; otherwise both
are functionally equivalent (same `USING` clause).

### F3 — Deterministic bootstrap-admin detection

**Defect:** `clickhouse/bootstrap.py:_has_admin_role_with_suffix` looks for
*any* role with the given suffix that holds `ROLE ADMIN` WGO at global
scope. If an operator manually grants `ROLE ADMIN WGO` to an unrelated
`<user>_USER` role (e.g., for diagnostics), bootstrap silently skips the
configured `CLICKHOUSE_ADMIN_USER` forever.

**Change:**

- Replace `_has_admin_role_with_suffix` with `_admin_already_bootstrapped`,
  which takes the *expected* role name (`f"{admin_user}_USER"` or
  `f"{admin_group}_GRP"`) and queries:
  ```sql
  SELECT count() FROM system.role_grants
  WHERE role_name = {expected:String}
    AND granted_role_name = {global_admin:String}
  ```
  Returns true iff the configured role itself already holds
  `iris_global_admin`. Tied to the configured name, immune to manual grants
  on unrelated roles.
- Update `bootstrap_admin` call sites accordingly.
- Add the docstring noting that re-runs with a new `CLICKHOUSE_ADMIN_USER`
  value DO bootstrap the new value (per-name, not global).

**Test (new in `tests/clickhouse/test_bootstrap_admin.py`):** pre-seed
`unrelated_USER` with `ROLE ADMIN WGO`, run `bootstrap_admin(admin_user="alice")`,
assert `alice_USER` is created with the admin grants. (Old heuristic would skip.)

### F4 — Clear `OAUTH_STATE_COOKIE` on `/login/callback` error

**Defect:** `auth/routes.py:login_callback` only deletes the state cookie
on the success path. On `AuthError`, the redirect to `/login?error=...`
keeps the signed state cookie alive for its full 10-minute TTL.

**Change:** in the `except AuthError` block of `login_callback`, build the
redirect response, call `response.delete_cookie(OAUTH_STATE_COOKIE)`, return.

**Test (new in `tests/auth/test_provider_oauth.py`):** simulate a callback
that fails state validation; assert the response has a `Set-Cookie` header
clearing `OAUTH_STATE_COOKIE`.

### F5 — Eliminate `*_impl` thunks in `clickhouse/handle.py`

**Defect:** `clickhouse/handle.py` (419 LOC) contains 24 wrapper functions of
the shape `async def X_impl(client, **kwargs): await asyncio.to_thread(X, client, **kwargs)`.
The layer adds no value; it triples the touch-points for adding a CH op.

**Change (Option A1 from design discussion):**

- Create new module `clickhouse/queries.py` containing the two functions with
  non-trivial async logic, renamed without the `_impl` suffix:
  - `query_as_user(http_client, *, username, sql, parameters=None, database=None) -> list[dict]`
  - `query_as_service(client, *, sql, parameters=None, database=None) -> QueryResult`
- Delete `clickhouse/handle.py` entirely.
- In `auth/identity.py`, replace the bulk import from `iris.clickhouse.handle`
  with direct imports from the relevant sync modules
  (`grants`, `policies`, `users`, `audit`) plus `queries`. Each Session
  method body becomes one line:
  ```python
  async def grant_reader(self, username: str) -> None:
      await asyncio.to_thread(
          grant_tier_to_user, self.client,
          database=self.database, tier=TIER_DBREADER, username=username,
      )
  ```
- Update the two test files that import from `iris.clickhouse.handle`:
  - `tests/clickhouse/test_handle.py` and `tests/clickhouse/test_handle_integration.py`
    change their imports from `from iris.clickhouse.handle import query_as_user_impl, query_as_service_impl`
    to `from iris.clickhouse.queries import query_as_user, query_as_service`
    and rename references accordingly.
- Update `iris.clickhouse.__init__` if `handle` was re-exported anywhere
  (it is not, per current source — sanity-check during implementation).
- Update CLAUDE.md: the line "Session methods use top-level imports of
  `iris.clickhouse.handle.*_impl`" needs to be rewritten to reflect the new
  shape (Session methods import from the sync modules + `queries`).

**Test:** existing test suite covers the Session method surface end-to-end
(via `tests/clickhouse/test_admin_handle.py`, `test_creator_handle.py`,
`test_handle_integration.py`, `test_login_provisioning.py`). No new tests
required; passing the existing suite is the acceptance signal.

### F6 — Remove `query_as_user` from `AuthSession` (Liskov fix)

**Defect:** `auth/identity.py:DatabaseSession.query_as_user` overrides the
parent's `AuthSession.query_as_user(database=...)` while dropping the
`database=` kwarg, requiring `# pyright: ignore[reportIncompatibleMethodOverride]`.

**Change:**

- Remove `query_as_user` from `AuthSession`. (Verified: no production route
  calls `session.query_as_user()` on a plain `AuthSession`; tests that
  exercise `query_as_user_impl` import the function directly and continue
  to do so via `clickhouse/queries.py`.)
- In `DatabaseSession`, `query_as_user` is no longer an override — its
  signature stands alone, no Liskov violation, no pyright ignore.
- Delete the `# Intentional Liskov violation:` block (D10).

**Test:** the existing tests for `DatabaseSession.query_as_user` continue to
pass. No new tests; defect is structural.

### F7 — Replace `assert` with explicit raise in `build_provider`

**Defect:** `auth/providers/__init__.py:build_provider` uses `assert
settings.X is not None` to discharge a runtime invariant. `python -O` strips
asserts; in optimized mode, a malformed config would None-deref later.

**Change:** for each branch, replace
```python
assert settings.mock is not None
return MockProvider(settings.mock)
```
with
```python
if settings.mock is None:
    raise RuntimeError("AUTH_METHOD=mock requires settings.mock to be configured")
return MockProvider(settings.mock)
```
(and analogous for ldap/oauth).

**Test (new in `tests/auth/test_config.py` or `tests/auth/test_provider_*`):**
construct an `AuthSettings` with `method="mock"` and `mock=None` (bypassing
`from_env`), assert `build_provider` raises `RuntimeError` (not
`AssertionError`).

### F8 — Derive OAuth state-signing key from `client_secret`

**Defect:** `auth/providers/oauth.py:OAuthProvider.__init__` constructs
`URLSafeTimedSerializer(settings.client_secret, salt="iris-oauth-state")`.
The state-signing key is literally the OAuth client secret. Compromise of
one fully compromises the other.

**Change (per user direction: always derive, no env var):**

- In `OAuthProvider.__init__`, compute:
  ```python
  derived_key = hashlib.sha256(
      b"iris-oauth-state-signing-v1:" + settings.client_secret.encode()
  ).digest()
  self._signer = URLSafeTimedSerializer(derived_key, salt="iris-oauth-state")
  ```
- The version tag (`v1`) lets us rotate the derivation later without
  invalidating in-flight state cookies during a no-downtime upgrade.
- Document the derivation in the OAuthProvider class docstring (D6).

**Test (new in `tests/auth/test_provider_oauth.py`):** construct
`OAuthProvider`, assert that the value passed into the serializer (or a
public attribute exposing the derived key for testability) is NOT byte-equal
to `settings.client_secret.encode()`. Round-trip a state payload to confirm
the signer still functions.

## Documentation fixes

| ID | File | Change |
|---|---|---|
| D1 | `auth/providers/__init__.py:13,18` | delete the two `# not implemented yet (Task 10/11)` comments — both providers are fully implemented |
| D2 | `clickhouse/identifiers.py:policy_name` | docstring covers 16-char digest (F2), collision space, and `IF NOT EXISTS` interaction in `add_row_policy` |
| D3 | `clickhouse/bootstrap.py` | new function from F3 gets a clear docstring; the obsoleted `_has_admin_role_with_suffix` is removed |
| D4 | `auth/sessions.py:SessionStore.__init__` | add docstring covering: parameters, the `asyncio.Lock` + single-connection model, `WAL` rationale, lifecycle (`close()` is idempotent and required) |
| D5 | `auth/exceptions.py:_wants_html` | one-line comment: "treats `Accept: text/html` as HTML; bare `*/*` (default browser fetch) falls through to the JSON branch" |
| D6 | `auth/providers/oauth.py:OAuthProvider` | class docstring covering: lazy discovery (sync httpx client used because PyJWKClient bypasses the test transport), the dual sync/async client design, the F8 derived signing key, and the documented limitation that JWKS rotation requires app restart |
| D7 | `auth/providers/ldap.py:LDAPProvider` | class docstring covering: dynamic ldap3 typing (the file-level pyright suppression), the `_USERNAME_RE` whitelist defending the bind DN template, and the two-stage `bind → search` flow |
| D8 | `templates.py` | one-line module docstring: "Shared `Jinja2Templates` instance for both root-level (`index.html`) and auth-flow (`auth/*.html`) templates" |
| D9 | All `logger.info(...)` call sites | standardize key=value vocabulary across `auth/routes.py`, `clickhouse/install.py`, `clickhouse/bootstrap.py`, `auth/providers/*.py`. Canonical keys: `subject=`, `username=`, `display_name=`, `groups=`, `remote_addr=`, `method=` (login method), `reason=` (failure token), `session_id=`. Existing `user=` (where it carries display_name) becomes `display_name=`. |
| D10 | `auth/identity.py` | delete the `# Intentional Liskov violation:` block — obsoleted by F6 |

## Acceptance criteria

- `uv run pytest` passes (full suite, including new tests for F1–F4, F7, F8).
- `uv run ruff check` clean.
- `uv run basedpyright --level error` clean.
- `uv run basedpyright --level warning` clean.
- `clickhouse/handle.py` no longer exists.
- `# pyright: ignore[reportIncompatibleMethodOverride]` no longer present in `auth/identity.py`.
- `assert settings.X is not None` no longer present in `auth/providers/__init__.py`.
- The OAuth state signer's key, when introspected, differs from the configured `client_secret`.

## Implementation order

Per the agreed risk profile (bug fixes incremental, refactor atomic):

1. **D1** — delete stale comments. Trivial standalone commit.
2. **F1 + lifespan test + D4** — shutdown hook registry. One commit.
3. **F2 + identifier test + D2** — 16-char digest. One commit.
4. **F3 + bootstrap test + D3** — deterministic bootstrap detection. One commit.
5. **F4 + oauth callback test + D5** — state cookie cleanup; bundle the small `_wants_html` comment. One commit.
6. **F7 + config test + D8** — explicit raise; bundle the `templates.py` docstring. One commit.
7. **F8 + oauth signer test + D6 + D7** — derived signing key; bundle the OAuth and LDAP class docstrings. One commit.
8. **F5 + F6 + D10 + CLAUDE.md update + D9** — atomic refactor: handle.py deletion, queries.py creation, AuthSession Liskov fix, logger key normalization. One commit (matches CLAUDE.md "atomic refactor" pattern).

Total: **8 commits.** Each is independently green; tests pass at every step.

## Out of scope

The following items from the catalog are NOT addressed here. They belong in
future specs.

- Medium-severity items: per-user pruning race in multi-process deploys
  (Brittle), `_ensure_discovered` sync-in-async (Brittle), `_safe_next`
  URL-encoding gap (Brittle), `persist_data` last-write-wins (Brittle),
  duplicate `_get_bool`/`_required` env helpers (Smell), `Any`-typed Session
  fields (Smell), triple-cast in oauth (Smell), revoke-creates-role smell,
  `claims["sub"]` un-guarded (Unsound), mutable `data` dict (Unsound),
  `OIDCSettings` repr leak (Security), log injection via `display_name`
  (Security), `_safe_next` URL-decoding (Security).
- All Low-severity items.
- File organization: empty `auth/authz/` deletion, `session.py`/`sessions.py`
  rename, `identity.py` split, `clickhouse/handle.py` further split (the
  current spec deletes it instead), `_form.py` template rename, `templates.py`
  fold-in, `install` symbol disambiguation.
- Entity naming: `Session`/`AuthSession` alias clarification, `*_impl` suffix
  (F5 removes the suffix as a side effect), `add_admin_user_impl` →
  `grant_admin_to_user_impl` (also obsoleted by F5), `init_user_rights` →
  `provision_user`, `Rights.is_admin`/`has_*` verb consistency,
  `_safe_next` → `safe_next_url`, `_ch_refs` → `_clickhouse_refs`.

## Risks

- **F5 is the largest blast radius** (~26 method bodies edited; one module
  deleted; two test files with import updates). Mitigated by: tests cover the
  Session method surface end-to-end via the testcontainer suite.
- **F2 changes policy names for new policies.** Old deployed CH instances
  retain legacy 8-char-suffixed policies; new code creates 16-char-suffixed
  policies. Re-running `add_row_policy` for an existing
  `(db, table, role, value)` creates a duplicate next to the legacy one.
  Acceptable for v1; flagged in the F2 commit message.
- **F3 changes which configurations trigger bootstrap.** Operators who relied
  on the old heuristic to suppress bootstrap by manually pre-creating an
  admin role on a different name will see the configured `CLICKHOUSE_ADMIN_USER`
  bootstrapped on next start. This is the *intended* behavior change; flagged
  in the F3 commit message.
- **F8 is silently breaking for any in-flight OAuth state cookies at upgrade
  time.** Cookies signed with the old key (the raw `client_secret`) become
  unverifiable; affected users see a single `oauth_state` error and
  re-trigger login. State cookies have a 10-min TTL so the impact window is
  short. Acceptable; flagged in commit message.
