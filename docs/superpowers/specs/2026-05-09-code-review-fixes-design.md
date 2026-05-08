# Code-review fixes — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

A full-codebase review (auth + clickhouse subsystems) surfaced 13 actionable findings spanning correctness, security, brittle patterns, and documentation drift. The user prioritized which to fix and made the architectural calls: keep the no-CH test mode (typed-Optional refs), prefer lazy async-safe OIDC discovery over a new lifespan hook list, and defer the public-API dedup of grant/revoke methods. This spec captures the agreed fixes as four sequenced phases, each landable as a single commit/PR with a green test suite.

The dedup work (review items 4.1 / 4.3) is **out of scope** for this spec — see "Out of scope" below.

## Goals

- Close the high-impact security gaps (OIDC identity source, IdP claim trust, CSRF cookie reuse, SQLite session refresh TOCTOU, tier-revoke role leak).
- Fix the two correctness bugs (`list_admin_members` shape, dead branch in `_row_to_session`).
- Tighten typing on the session "service-locator" without dropping the no-CH test mode.
- Remove three stale module references and prune redundant docstrings.

## Non-goals

- No new functional features.
- No public-API changes to `DatabaseAdminSession` or `iris.clickhouse.grants`. Routes that today call `session.grant_reader(name)` etc. continue to work unchanged.
- No removal of `build_app(install_clickhouse=False)`.

## Out of scope (deferred)

- **4.1 / 4.3 — dedup of the 12 `DatabaseAdminSession` methods and 6 grants.py functions.** Reviewer's recommendation, but the user is not convinced of the value. Can be revisited later as a standalone refactor; nothing in this spec depends on it.

---

## Phase 1 — Documentation & no-op cleanups

**Scope:** docstrings, comments, one-line behavior fixes with no logic change.
**Files touched:** `iris/__init__.py`, `iris/app.py`, `iris/templates.py`, `iris/auth/__init__.py`, `iris/auth/sessions.py`, `iris/auth/routes.py`, `iris/clickhouse/__init__.py`, `iris/clickhouse/install.py`, `iris/clickhouse/users.py`, `iris/clickhouse/grants.py`.

### Items

- **6.1 — stale references.**
  - `iris/clickhouse/__init__.py:6` — drop the sentence about `*_impl` functions in `iris.clickhouse.handle` (module deleted in commit `ffa5391`).
  - `iris/clickhouse/install.py:4` — drop "see iris.clickhouse.handle for why both are needed".
  - `iris/clickhouse/users.py:62` — drop the "After dropping CLICKHOUSE_SERVICE_ADMIN_USER" historical aside (env var no longer exists).
- **6.2 — `from __future__ import annotations`** added to: `iris/__init__.py`, `iris/app.py`, `iris/templates.py`, `iris/auth/__init__.py`, `iris/clickhouse/__init__.py`. Brings them in line with the rest of the codebase.
- **6.3 — prune redundant docstrings.** `grants.py` has five functions whose docstrings end with "Idempotent." after restating the function name. Per project convention ("default to writing no comments"), drop the empty-calorie text but keep the WHY-comments (e.g. the `_ensure_role` enumeration-defense rationale stays).
- **2.2 — dead branch.** `iris/auth/sessions.py:70-71` collapses to `rights = rights_from_dict(json.loads(row["rights_json"]))`. The schema's `NOT NULL DEFAULT '{}'` makes the existing `if … else {}` unreachable.
- **1.9 — login POST 405 missing `Allow` header.** `iris/auth/routes.py:110` returns `Response(status_code=405, headers={"Allow": "GET"})`.

### Risks

None. No behavior change beyond the 405 header (additive, spec-compliant).

### Tests

- Existing test suite must pass unchanged.
- Optional: a smoke test that POST `/login` against an OAuth-configured app returns 405 with `Allow: GET`.

---

## Phase 2 — Targeted security & correctness fixes

**Scope:** local changes; no API surface shifts.
**Files touched:** `iris/clickhouse/grants.py`, `iris/auth/sessions.py`, `iris/auth/csrf.py`, `iris/clickhouse/queries.py`, `iris/auth/identity.py`.

### Items

- **1.4 — drop `_ensure_role` from revoke paths.** `revoke_tier_from_user` and `revoke_tier_from_group` (`grants.py:87-107`) no longer pre-create the principal role. CH no-ops a `REVOKE` against a non-existent grantee, so this is purely state-leak elimination: revoking a tier from `attacker_supplied_username` will not silently materialize an empty role.
- **1.6 — wrap session refresh in `BEGIN IMMEDIATE`.** `_get_and_refresh_sync` (`sessions.py:188-214`) reshaped to:
  ```python
  self._conn.execute("BEGIN IMMEDIATE")
  try:
      row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
      if row is None:
          self._conn.execute("COMMIT")
          return None
      now = datetime.now(UTC)
      ...
      if expired:
          self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
          self._conn.execute("COMMIT")
          return None
      self._conn.execute("UPDATE sessions SET expires_at_ts = ? WHERE id = ?", ...)
      self._conn.execute("COMMIT")
      return ...
  except Exception:
      self._conn.execute("ROLLBACK")
      raise
  ```
  Mirrors the `_create_sync` pattern.
- **1.7 — CSRF cookie sanity check.** `mint_csrf_token` (`csrf.py:12-14`) becomes:
  ```python
  _CSRF_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

  def mint_csrf_token(request: Request) -> str:
      existing = request.cookies.get(CSRF_COOKIE_NAME, "")
      if existing and _CSRF_TOKEN_RE.fullmatch(existing):
          return existing
      return secrets.token_urlsafe(32)
  ```
  Prevents an attacker-supplied bogus cookie value from sticking around.
- **3.3 — type-aware param marshaling in `query_as_user`.** `iris/clickhouse/queries.py:48`:
  ```python
  def _marshal_param(v: object) -> str:
      if isinstance(v, bool):
          return "1" if v else "0"
      if isinstance(v, (int, float, str)):
          return str(v)
      if isinstance(v, datetime):
          return v.isoformat(timespec="seconds").replace("+00:00", "")
      raise TypeError(f"unsupported CH param type: {type(v).__name__}")
  ```
  `bool` check must come before `int` (Python `bool` is subclass of `int`). Helper is module-private.
- **2.1 — `list_admin_members` returns users + roles.** `iris/auth/identity.py:214-225` reshaped:
  ```python
  async def list_admin_members(self) -> list[dict[str, str]]:
      """Return everything granted the per-database admin role.

      Each entry is `{"kind": "user" | "role", "name": <str>}`. Includes
      direct user grantees and role grantees (e.g. group-roles holding the
      tier).
      """
      admin_role = tier_role_name(self.database, TIER_DBADMIN)
      def _sync() -> list[dict[str, str]]:
          rows = client.query(
              """
              SELECT user_name, role_name FROM system.role_grants
              WHERE granted_role_name = {r:String}
              """,
              {"r": admin_role},
          )
          out = []
          for row in rows.named_results():
              if row.get("user_name"):
                  out.append({"kind": "user", "name": cast(str, row["user_name"])})
              elif row.get("role_name"):
                  out.append({"kind": "role", "name": cast(str, row["role_name"])})
          return out
      return await asyncio.to_thread(_sync)
  ```

### Risks

- `tests/clickhouse/test_admin_handle.py::test_list_admin_members_returns_creator` will need to be updated for the new return shape (asserts the creator is present as `{"kind": "user", "name": <username>}` instead of a bare string).
- `BEGIN IMMEDIATE` slightly increases lock contention under heavy concurrent reads; expected impact negligible at this scale.
- `_marshal_param` raising `TypeError` on unsupported inputs is a behavior change: previously `str(v)` would coerce anything; now callers passing `None` etc. get a clear error. Since CH would have rejected the resulting string anyway, this is strictly better.

### Tests

- Update `test_list_admin_members_returns_creator` to assert the new shape.
- New: `test_csrf_malformed_cookie_minted_fresh` — set `iris_csrf=<garbage>` cookie, verify `mint_csrf_token` returns a fresh token.
- New: `test_marshal_param` — table-driven coverage of bool/int/float/str/datetime/None/object.
- New: `test_revoke_does_not_create_role` — call `revoke_tier_from_user` for a username that has no role in CH; assert no `<username>_USER` role exists afterwards.
- Optional: a multi-coroutine concurrency test for `_get_and_refresh_sync` showing that two concurrent refreshes don't produce overlapping writes.

---

## Phase 3 — OIDC hardening (integrated)

**Scope:** all in `iris/auth/providers/oauth.py`. Tests in `tests/auth/test_provider_oauth.py` and `tests/auth/integration/test_oauth_integration.py` get reshaped.
**Files touched:** `iris/auth/providers/oauth.py`, two test files.

### Items

- **1.2 — `assert` → explicit raise.** `_verify_id_token` (`oauth.py:234`):
  ```python
  if self._jwks is None:
      raise AuthError("oauth_exchange")
  ```
- **3.1 — lazy async-safe discovery.** Replace the sync-httpx `_ensure_discovered` with an async one guarded by an `asyncio.Lock`. Properties become inline reads inside the three async call sites (`begin`, `complete`, `exchange_code`). Sync `httpx.Client` and the `_client` attribute are deleted entirely.
  ```python
  def __init__(self, settings, *, _http_transport=None):
      ...  # async client setup unchanged
      self._discovery_lock = asyncio.Lock()
      self._discovered: dict[str, Any] | None = None
      self._jwks: jwt.PyJWKSet | None = None

  async def _ensure_discovered(self) -> dict[str, Any]:
      if self._discovered is not None:
          return self._discovered
      async with self._discovery_lock:
          if self._discovered is not None:
              return self._discovered
          discovery_url = self._settings.issuer_url.rstrip("/") + "/.well-known/openid-configuration"
          try:
              doc = (await self._async_client.get(discovery_url)).raise_for_status().json()
              jwks_doc = (await self._async_client.get(doc["jwks_uri"])).raise_for_status().json()
          except Exception as exc:
              logger.exception("auth: OIDC discovery failed")
              raise AuthError("oauth_discovery") from exc
          self._discovered = doc
          self._jwks = jwt.PyJWKSet.from_dict(jwks_doc)
          return doc
  ```
  Call sites:
  - `begin` awaits `_ensure_discovered` and reads `doc["authorization_endpoint"]`.
  - `complete` / `exchange_code` (via `_request_tokens`) does the same for `token_endpoint`.
  - `_fetch_userinfo` for `userinfo_endpoint`.
  All `@property` accessors and `build_authorize_url` (currently sync) are reshaped: `build_authorize_url` becomes async or takes the discovered doc as an argument. Cleaner: inline the URL construction into `begin` directly.
- **1.1 — id_token canonical sub + nonce + sub-match.**
  - `build_authorize_url` adds `nonce = secrets.token_urlsafe(16)` to the params and returns it alongside `state`/`verifier`.
  - `begin` signs `{"state", "verifier", "next", "nonce"}` into the state cookie.
  - `complete` reads the nonce out of the cookie and threads it to `exchange_code`.
  - `_verify_id_token` returns the decoded claims; `jwt.decode` adds `options={"require": ["sub", "iat", "exp", "aud", "iss", "nonce"]}` and verifies `claims["nonce"] == expected_nonce` after decode.
  - `exchange_code` orchestrates: verify id_token → fetch userinfo → assert `userinfo["sub"] == claims_id["sub"]`, raise `AuthError("oauth_sub_mismatch")` on mismatch → build `User` using id_token's `sub` plus userinfo's groups/preferred_username/name.
- **1.3 — IdP claim validation.** `_user_from_claims` (renamed `_user_from_id_and_userinfo` for clarity, or kept):
  ```python
  def _user_from_id_and_userinfo(
      self, *, id_claims: dict[str, Any], ui_claims: dict[str, Any]
  ) -> User:
      try:
          sub = str(id_claims["sub"])
      except KeyError as exc:
          raise AuthError("oauth_exchange") from exc
      raw_groups = ui_claims.get("groups", [])
      if not isinstance(raw_groups, list):
          logger.warning("auth: OIDC userinfo groups is not a list (got %s)", type(raw_groups).__name__)
          raw_groups = []
      groups = tuple(str(g) for g in raw_groups)
      if not groups:
          logger.warning("auth: OIDC userinfo had no `groups` claim")
      username = str(ui_claims.get("preferred_username") or sub)
      return User(
          subject=sub,
          username=username,
          display_name=str(ui_claims.get("name") or username),
          groups=groups,
      )
  ```

### Risks

- The OAuth integration test (Keycloak) needs the `nonce` claim mapper enabled (Keycloak default supports nonce). Mock-IdP tests in `test_provider_oauth.py` need the mock to echo the nonce back into the id_token and to put `sub` consistently in both id_token and userinfo.
- Removing the sync `httpx.Client` is a real reduction; double-check no test reaches into `provider._client` directly.
- The `_http_transport` shim's double-cast (`cast("httpx.AsyncBaseTransport", cast(object, _http_transport))`) is preserved — we still need it for the async client.
- Any caller depending on `provider.authorize_endpoint` etc. as a sync property breaks. Tests are the only such callers; they get rewritten to await `_ensure_discovered`.

### Tests

- Update `test_provider_oauth.py` mock IdP to:
  - Issue id_tokens that include `sub`, `nonce`, `iat`, `exp`, `aud`, `iss`.
  - Echo the request `nonce` into the id_token.
  - Put the same `sub` in userinfo.
- Update Keycloak integration test for nonce flow (Keycloak supports it natively when `nonce` is sent on `/auth`; verify response carries it through to id_token).
- New tests:
  - `test_oauth_sub_mismatch_rejected` — userinfo returns a different `sub`; expect `AuthError("oauth_sub_mismatch")`.
  - `test_oauth_nonce_mismatch_rejected` — alter the cookie nonce; expect `AuthError("oauth_exchange")`.
  - `test_oauth_groups_not_list_treated_as_empty` — userinfo returns `"groups": "admin"` (string); user has empty groups, warning logged.
  - `test_oauth_missing_sub_rejected` — id_token has no `sub`; expect `AuthError("oauth_exchange")`.
  - `test_oauth_concurrent_first_requests_discover_once` — two coroutines hit `begin` simultaneously on a fresh provider; assert only one discovery network call.

---

## Phase 4 — Service-locator typing

**Scope:** purely typing; no behavior change.
**Files touched:** `iris/auth/identity.py`, `iris/auth/deps.py`, possibly `tests/auth/conftest.py` if it constructs `AuthSession` literals.

### Items

- **3.2 — typed Optional refs + `_ch()` helper.**
  - `AuthSession` field types change:
    ```python
    client: Client | None = field(repr=False, compare=False)
    http_client: httpx.AsyncClient | None = field(repr=False, compare=False)
    settings: ClickHouseSettings | None = field(repr=False, compare=False)
    store: SessionStore = field(repr=False, compare=False)  # always present
    ```
  - New private helper on `AuthSession`:
    ```python
    def _ch(self) -> tuple[Client, httpx.AsyncClient, ClickHouseSettings]:
        if self.client is None or self.http_client is None or self.settings is None:
            raise RuntimeError(
                "ClickHouse not installed; "
                "this method requires build_app(install_clickhouse=True)"
            )
        return self.client, self.http_client, self.settings
    ```
  - All CH-using methods on subclasses replace ad-hoc `self.client` reads with `client, http_client, settings = self._ch()` at the top.
  - `from typing import Any` removed from `identity.py` and `deps.py` where no longer needed (the `data: dict[str, Any]` field still requires it).
  - `_to_auth_session` in `deps.py` keeps its current shape; the only change is the return type hints flow through.

### Risks

- Tests that construct `AuthSession` directly need typed refs. `conftest.py` likely has helpers; update once and the rest follows.
- Pyright may flag previously-hidden type errors. Each one is a real bug-finding opportunity; budget time to fix or suppress with reason.

### Tests

- Existing suite must pass unchanged. No new tests required (typing-only change).
- Run `uv run basedpyright --level error` and `--level warning` and resolve any new findings before merging.

---

## Test impact summary

| Phase | Tests touched | New tests |
|---|---|---|
| 1 | none | optional 405-Allow header smoke |
| 2 | `test_list_admin_members_returns_creator` | CSRF malformed cookie, `_marshal_param` table, revoke-no-leak, optional concurrency |
| 3 | `test_provider_oauth.py` (mock IdP), Keycloak integration test | sub-mismatch, nonce-mismatch, groups-not-list, missing-sub, concurrent-first-discover |
| 4 | possibly `conftest.py` AuthSession constructors | none |

## Rollout

Each phase is one commit and one PR. Order is fixed: 1 → 2 → 3 → 4. Phases 1 and 2 are mechanically cheap; Phase 3 is the only one with real risk and gets its own review window. Phase 4 follows easily on Phase 3 because pyright errors will be obvious once they appear.

## Non-decisions for the implementation plan

- Whether `_marshal_param` lives in `queries.py` or its own helper module — let the implementation plan decide.
- Whether to rename `_user_from_claims` to `_user_from_id_and_userinfo` — implementation-detail call.
- Exact wording of pruned docstrings — implementation-detail call.
