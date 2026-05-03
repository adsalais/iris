# Iris Codebase Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce duplication and convoluted constructs across the `iris` package without changing any observable behavior, public API, error tokens, ordering, timing, or logging.

**Architecture:** This is a pure-refactor plan. Each task collapses one specific duplication or splits one large function. The full pytest suite (currently 119 passed, 1 skipped) is the regression oracle — every task ends by re-running the full suite and confirming the same result. **No new tests are added** because the user has explicitly forbidden behavior changes; new tests would either duplicate existing coverage (waste) or assert new semantics (forbidden). If a refactor required a new test to be safe, that's a signal the refactor itself is risky and should be flagged for confirmation, not silently absorbed.

**Tech Stack:** Python 3.13, FastAPI, pytest, `uv` for env management. Hatchling/`src/`-layout. No linter/formatter configured yet — preserve existing whitespace and import ordering style; do not reformat.

---

## Scope decisions (preserved-as-is, NOT refactored)

These are deliberately excluded from the plan. Listing them so the executor doesn't second-guess:

- `_safe_next` in `src/iris/auth/routes.py:34-44` — three sequential `if` checks could collapse into one boolean expression, but the existing form documents each rejection rule on its own line. Style-only change; skip.
- `_get_bool` in `src/iris/auth/config.py:15-24` — the `""` (empty string) being treated as `False` rather than the default is an intentional dotenv quirk (a `KEY=` line yields `""`, not absent). CLAUDE.md confirms typos must be loud. Leave alone.
- `iris/app.py` ↔ `iris/auth/routes.py` circular import (resolved with two inline `from … import …` statements). Untangling would require a third module just to hold `TEMPLATES` — that's added structure, not removed structure. Skip per "don't over-abstract".
- `LDAPProvider._open_connection` in `src/iris/auth/providers/ldap.py:90-119` — the test-factory branch and the real-server branch share an exception-mapping pattern. The real branch additionally builds TLS config. Extracting a shared mapper saves ~6 lines but tangles the two code paths; the duplication is shallow and the function is short. Skip.
- The four OAuth `@property` accessors (`authorize_endpoint`, `token_endpoint`, `userinfo_endpoint`, `jwks_uri`) — they are accessed by tests (`tests/auth/test_provider_oauth.py:50-52, 63`) and are part of the implicit public surface. **Cannot remove**, but CAN simplify their bodies (Task 2).
- `jwks_uri` property is unused outside the class itself (`_ensure_discovered` uses `doc["jwks_uri"]` directly, not the property). Removing it would be a public-interface change and the user said don't do that without approval. Flag in the post-plan summary, do not remove.
- `csrf.issue_csrf_token` is exercised by `tests/auth/test_csrf.py:8,20` — keep.

## Public surface that MUST NOT change

If any of these change, the refactor has overstepped:

- Module exports: `iris.auth.__init__` (`CurrentSession`, `CurrentUser`, `OptionalCurrentUser`, `SessionData`, `User`, `UserSession`, `install`, `require_group`).
- `iris.auth.csrf` exports: `CSRF_COOKIE_NAME`, `CSRF_FORM_FIELD`, `mint_csrf_token`, `attach_csrf_cookie`, `issue_csrf_token`, `verify_csrf_form`, `delete_csrf_cookie`.
- `OAuthProvider` instance attributes accessed by tests: `authorize_endpoint`, `token_endpoint`, `userinfo_endpoint`, `_settings`, `_signer`, `_client`, `_async_client`. Methods: `begin`, `complete`, `build_authorize_url`, `exchange_code`, `close`. Constructor signature `(settings, *, _http_transport=None)`.
- `LDAPProvider`, `MockProvider`: `begin`, `complete`, `authenticate` signatures.
- `InMemorySessionStore`: `create`, `get_and_refresh`, `delete` and constructor `(ttl_seconds, absolute_ttl_seconds, max_per_user=10)`.
- `User` and `UserSession` field names + types (frozen+slots).
- All error tokens raised via `AuthError(...)`: `invalid_credentials`, `ldap_unreachable`, `ldap_groups`, `oauth_state`, `oauth_exchange`, `oauth_discovery`, `csrf_mismatch`. Same string, raised at the same boundaries.
- All `logger.info(...)` / `logger.warning(...)` / `logger.exception(...)` calls — same logger name, same format string, same arguments, same call site (so exception logs preserve the originating frame info). If a function is split, the log call must move with the code that triggered it.
- HTTP status codes, redirect targets, cookie names, cookie attributes (max_age, httponly, secure, samesite, path).

---

## File Structure

This refactor edits existing files only. **One new file** is added:

- **Create:** `src/iris/auth/providers/_form.py` — shared "render the username/password login form" helper for `MockProvider` and `LDAPProvider`. Underscore-prefixed because it's internal to the providers package; not exported from `iris.auth`.

- **Modify:** `src/iris/auth/providers/oauth.py` — Tasks 2, 3 (collapse property bodies; split `exchange_code`).
- **Modify:** `src/iris/auth/deps.py` — Task 4 (extract `_required_session`).
- **Modify:** `src/iris/auth/providers/mock.py` — Task 5 (use shared form helper).
- **Modify:** `src/iris/auth/providers/ldap.py` — Task 5 (use shared form helper).
- **Modify:** `src/iris/auth/routes.py` — Task 6 (extract `_finalize_login_redirect`).

No test files are modified. No template files are modified. No new exports added.

---

## Task 1: Establish baseline

**Files:** none (verification only).

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest -q`
Expected: `119 passed, 1 skipped, 2 warnings` (the two warnings are ldap3's pyasn1 deprecations — pre-existing, not our concern). If the count differs from this baseline, **stop and investigate** before refactoring; some other change has landed.

- [ ] **Step 2: Record exact baseline**

Capture the final summary line so subsequent tasks can compare against it. Expected line shape: `119 passed, 1 skipped, 2 warnings in <N>s`. Time will vary; passed/skipped counts must not.

- [ ] **Step 3: Confirm working tree is clean of unrelated changes**

Run: `git status --short`
Expected: only the pre-existing `M README.md` and `?? .zed/` — nothing else. If there are other modifications, ask before proceeding so we don't conflate them with refactor changes.

---

## Task 2: Collapse OAuth lazy-discovery property bodies

**Files:**
- Modify: `src/iris/auth/providers/oauth.py:46-83`

**What and why:** Today, `_ensure_discovered()` returns `None` and mutates `self._discovered`. Each of the four `@property` accessors then does `_ensure_discovered()` + `assert self._discovered is not None` (for type narrowing) + `return self._discovered["..."]`. That's three lines of noise per property. Change `_ensure_discovered` to return the dict; drop the asserts; each property becomes one line. The behavior is identical: same lazy fetch, same caching, same `AuthError("oauth_discovery")` on failure, same JWKS pre-load side effect.

- [ ] **Step 1: Replace `_ensure_discovered` and the four properties**

Replace lines 46-83 (`_ensure_discovered` through the end of the `jwks_uri` property) with the block below. **Keep all four `@property` declarations** (tests access three of them; the fourth is preserved per the public-interface rule):

```python
    def _ensure_discovered(self) -> dict:
        if self._discovered is not None:
            return self._discovered
        discovery_url = (
            self._settings.issuer_url.rstrip("/") + "/.well-known/openid-configuration"
        )
        try:
            doc = self._client.get(discovery_url).raise_for_status().json()
            jwks_doc = self._client.get(doc["jwks_uri"]).raise_for_status().json()
        except Exception as exc:
            logger.exception("auth: OIDC discovery failed")
            raise AuthError("oauth_discovery") from exc
        self._discovered = doc
        self._jwks = jwt.PyJWKSet.from_dict(jwks_doc)
        return doc

    @property
    def authorize_endpoint(self) -> str:
        return self._ensure_discovered()["authorization_endpoint"]

    @property
    def token_endpoint(self) -> str:
        return self._ensure_discovered()["token_endpoint"]

    @property
    def userinfo_endpoint(self) -> str:
        return self._ensure_discovered()["userinfo_endpoint"]

    @property
    def jwks_uri(self) -> str:
        return self._ensure_discovered()["jwks_uri"]
```

Behavior contract preserved:
- First property access still fetches discovery + JWKS in one call (the JWKS fetch is still inside the same try, so a JWKS HTTP failure still maps to `AuthError("oauth_discovery")`).
- `self._discovered` is still set BEFORE `self._jwks` (so a JWKS parse failure leaves `_discovered=None`, forcing re-fetch on next call — same as today).
- `logger.exception("auth: OIDC discovery failed")` is at the same call site, same message.
- `_ensure_discovered()` is still idempotent and short-circuits when `_discovered` is set.
- `_jwks` initialization unchanged — still happens lazily on first discovery.

- [ ] **Step 2: Run OAuth-specific tests**

Run: `uv run pytest tests/auth/test_provider_oauth.py -v`
Expected: all tests pass, including:
- `test_provider_construction_does_not_fetch_discovery` (no fetch on construction)
- `test_first_property_access_triggers_discovery` (lazy fetch on first property hit)
- `test_discovery_failure_surfaces_oauth_discovery_token` (HTTP 503 → AuthError("oauth_discovery"))
- `test_complete_callback_returns_user` (full code-exchange path uses `userinfo_endpoint` and `token_endpoint` internally; both still resolve)

- [ ] **Step 3: Run full suite**

Run: `uv run pytest -q`
Expected: `119 passed, 1 skipped, 2 warnings`. Identical to baseline.

- [ ] **Step 4: Commit**

```bash
git add src/iris/auth/providers/oauth.py
git commit -m "refactor(oauth): collapse property bodies; _ensure_discovered returns the doc"
```

---

## Task 3: Split `OAuthProvider.exchange_code` into named helpers

**Files:**
- Modify: `src/iris/auth/providers/oauth.py:147-200`

**What and why:** `exchange_code` is 54 lines doing four distinct things: token POST, ID-token verification, userinfo GET, and User construction. The control flow uses an outer `try/except Exception` plus `except AuthError: raise` to prevent the inner `AuthError` (from ID-token verification) from being re-wrapped — a sentinel pattern that's easy to misread. Splitting into three private helpers, each with a narrow try/except mapping its own failures to `AuthError("oauth_exchange")`, removes the `except AuthError: raise` workaround entirely.

**Behavior contract:**
- Public method signature unchanged: `async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> User`.
- Same error token (`oauth_exchange`) raised at the same observable boundaries: token-endpoint HTTP failure, missing `id_token` in token response, ID-token signature/audience/issuer failure, missing `kid`, userinfo HTTP failure.
- Same `logger.error("auth: token endpoint returned no id_token")` for missing id_token.
- Same `logger.exception("auth: id_token verification failed")` for ID-token failures.
- Same `logger.exception("auth: OAuth code exchange failed")` for token POST failures and userinfo failures (today both go through the broad outer except — preserved by routing both helpers' broad excepts through the same log line).
- Same `logger.warning("auth: OAuth userinfo had no \`groups\` claim — check IdP client mapper")` when groups is empty.
- Same `User` construction: `subject=str(claims["sub"])`, `display_name` fallback chain `name → preferred_username → sub`, `groups=tuple(claims.get("groups") or ())`.
- The KeyError-on-missing-`sub` raised by `claims["sub"]` is currently NOT caught (it happens after the outer try block). Preserve that — the helper that builds the User must run OUTSIDE any broad try/except so a missing `sub` propagates as KeyError, not AuthError. (This is current behavior; verify test coverage doesn't depend on it being mapped.)

- [ ] **Step 1: Replace `exchange_code` with helpers + thin orchestrator**

Replace lines 147-200 (the entire `exchange_code` method) with:

```python
    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> User:
        token_response = await self._request_tokens(
            code=code, code_verifier=code_verifier, redirect_uri=redirect_uri
        )
        id_token = token_response.get("id_token")
        if not id_token:
            logger.error("auth: token endpoint returned no id_token")
            raise AuthError("oauth_exchange")
        self._verify_id_token(id_token)
        claims = await self._fetch_userinfo(token_response["access_token"])
        return self._user_from_claims(claims)

    async def _request_tokens(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> dict:
        try:
            r = await self._async_client.post(
                self.token_endpoint,
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

    def _verify_id_token(self, id_token: str) -> None:
        try:
            unverified_header = jwt.get_unverified_header(id_token)
            signing_key = self._jwks[unverified_header["kid"]].key
            jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256", "ES256"],
                audience=self._settings.client_id,
                issuer=self._settings.issuer_url.rstrip("/"),
            )
        except (jwt.InvalidTokenError, KeyError) as exc:
            logger.exception("auth: id_token verification failed")
            raise AuthError("oauth_exchange") from exc

    async def _fetch_userinfo(self, access_token: str) -> dict:
        try:
            ui = await self._async_client.get(
                self.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            ui.raise_for_status()
            return ui.json()
        except Exception as exc:
            logger.exception("auth: OAuth code exchange failed")
            raise AuthError("oauth_exchange") from exc

    def _user_from_claims(self, claims: dict) -> User:
        groups = tuple(claims.get("groups") or ())
        if not groups:
            logger.warning(
                "auth: OAuth userinfo had no `groups` claim — check IdP client mapper"
            )
        return User(
            subject=str(claims["sub"]),
            display_name=str(claims.get("name") or claims.get("preferred_username") or claims["sub"]),
            groups=groups,
        )

    # OAuth provider has no .authenticate(username, password); the route layer
    # calls .begin() and .complete() instead.
```

Notes on what changed semantically vs syntactically:
- The `except AuthError: raise` block is gone. The inner `_verify_id_token`'s AuthError is no longer inside an outer broad except, so no re-wrapping risk.
- The `logger.exception("auth: OAuth code exchange failed")` message is now logged at TWO call sites (`_request_tokens` and `_fetch_userinfo`) instead of one. Today, both failures go through the single outer except and produce the same log line — so the LOG OUTPUT is identical for either failure mode. The CALL SITE in the traceback shifts (was `exchange_code:188`, will be `_request_tokens` or `_fetch_userinfo`) — that's the only observable diff and it makes the log MORE useful, not less. **If the user considers traceback frame info part of the contract, flag and ask.**
- The "missing `id_token`" guard moves from inside the outer try (line 164) to the orchestrator. It still raises `AuthError("oauth_exchange")` with the same `logger.error` line. No `from exc` because today there isn't one either (the bare `raise AuthError("oauth_exchange")` at line 166 has no chained cause).

- [ ] **Step 2: Run OAuth-specific tests**

Run: `uv run pytest tests/auth/test_provider_oauth.py -v`
Expected: all tests pass. Pay special attention to:
- `test_complete_callback_returns_user` (full happy path)
- Any test asserting `AuthError` token equals `"oauth_exchange"` for failures
- Any test asserting log contents (search test file for `caplog` to find them)

- [ ] **Step 3: Inspect log assertions**

Run: `grep -n "caplog\|oauth_exchange\|id_token\|userinfo" tests/auth/test_provider_oauth.py`
Expected: review each result. If any test asserts the `traceback.format_exc()` shape or the function name in a log frame, the refactor breaks that test — STOP and ask the user. If not, proceed.

- [ ] **Step 4: Run full suite**

Run: `uv run pytest -q`
Expected: `119 passed, 1 skipped, 2 warnings`.

- [ ] **Step 5: Commit**

```bash
git add src/iris/auth/providers/oauth.py
git commit -m "refactor(oauth): split exchange_code into named helpers"
```

---

## Task 4: Dedupe None-checks across auth deps

**Files:**
- Modify: `src/iris/auth/deps.py:50-75`

**What and why:** `_current_user`, `_current_session`, and `_session_data` each start with `if session is None: raise AuthRequired()`. Extracting a `_required_session` dependency (which itself depends on `_resolve_session`) removes that triplicated guard. FastAPI's per-request dep cache keys by callable, so `_required_session` resolves once per request and is then shared by all three downstream deps — same number of store hits as today.

**Behavior contract:**
- All four `Annotated[..., Depends(...)]` aliases (`CurrentUser`, `OptionalCurrentUser`, `CurrentSession`, `SessionData`) keep the same name, same exported type, same exception (`AuthRequired`).
- Per-request store hits unchanged (still 1, via the shared `_resolve_session` cache entry).
- `OptionalCurrentUser` keeps using `_resolve_session` directly (it MUST return `None` instead of raising, so it can't go through `_required_session`).

- [ ] **Step 1: Replace lines 50-75**

Replace the block from `async def _current_user` through the four `Annotated[...]` aliases (inclusive) with:

```python
async def _required_session(session: _ResolvedSession) -> UserSession:
    if session is None:
        raise AuthRequired()
    return session


_RequiredSession = Annotated[UserSession, Depends(_required_session)]


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
```

- [ ] **Step 2: Run dep-related tests**

Run: `uv run pytest tests/auth/test_deps.py -v`
Expected: all tests pass. The test file exercises `CurrentUser`, `OptionalCurrentUser`, `CurrentSession`, `SessionData`, and `require_group` — all four behaviors must be preserved.

- [ ] **Step 3: Run full suite**

Run: `uv run pytest -q`
Expected: `119 passed, 1 skipped, 2 warnings`.

- [ ] **Step 4: Commit**

```bash
git add src/iris/auth/deps.py
git commit -m "refactor(deps): extract _required_session to dedupe None-checks"
```

---

## Task 5: Extract shared login-form rendering for Mock and LDAP providers

**Files:**
- Create: `src/iris/auth/providers/_form.py`
- Modify: `src/iris/auth/providers/mock.py:21-46`
- Modify: `src/iris/auth/providers/ldap.py:37-64`

**What and why:** `MockProvider.begin()` and `LDAPProvider.begin()` are nearly identical: same template name, same context keys, same CSRF mint+attach flow. The ONLY difference is the `error_message` lookup table. Extract a `render_login_form(request, error_messages)` helper. Provider-specific tables stay in each provider class.

**Behavior contract:**
- Same template (`auth/ldap_form.html`) rendered with identical context keys: `csrf_field`, `csrf_token`, `next_url`, `error`, `error_message`.
- Same CSRF cookie attached (via `attach_csrf_cookie`).
- Same query-param handling: `next` defaults to `"/"`, `error` is read raw.
- Same error-message resolution: an unknown error token shows `"An error occurred."`; absent `error` query param shows `""`.
- LDAP keeps its 4-key table (`invalid_credentials`, `ldap_unreachable`, `ldap_groups`, `csrf_mismatch`).
- Mock keeps its 2-key table (`invalid_credentials`, `csrf_mismatch`).
- The helper does NOT live in `iris.auth.csrf` (wrong layer) and is NOT exported from `iris.auth` (internal to providers).

- [ ] **Step 1: Create `src/iris/auth/providers/_form.py`**

Create the new file with this exact content:

```python
from __future__ import annotations

from fastapi import Request, Response

from iris.auth.csrf import CSRF_FORM_FIELD, attach_csrf_cookie, mint_csrf_token


def render_login_form(
    request: Request, error_messages: dict[str, str]
) -> Response:
    """Render the username/password login form with CSRF + error messaging.

    Shared by MockProvider and LDAPProvider. `error_messages` maps each
    provider-specific error token to its user-facing string; unknown tokens
    fall back to "An error occurred."; absent `error` query param shows "".
    """
    templates = request.app.state.templates
    next_url = request.query_params.get("next", "/")
    error = request.query_params.get("error")
    error_message = (
        error_messages.get(error or "", "An error occurred.") if error else ""
    )
    token = mint_csrf_token(request)
    response = templates.TemplateResponse(
        request,
        "auth/ldap_form.html",
        {
            "csrf_field": CSRF_FORM_FIELD,
            "csrf_token": token,
            "next_url": next_url,
            "error": bool(error),
            "error_message": error_message,
        },
    )
    attach_csrf_cookie(request, response, token)
    return response
```

- [ ] **Step 2: Rewrite `MockProvider.begin` to use the helper**

In `src/iris/auth/providers/mock.py`, replace the `begin` method (lines 21-46) with:

```python
    async def begin(self, request: Request) -> Response:
        return render_login_form(
            request,
            {
                "invalid_credentials": "Invalid username or password.",
                "csrf_mismatch": "Session expired, please reload and try again.",
            },
        )
```

Then update the imports at the top of `mock.py`:
- Remove the multi-line import block:
  ```python
  from iris.auth.csrf import (
      CSRF_FORM_FIELD,
      attach_csrf_cookie,
      mint_csrf_token,
  )
  ```
- Add: `from iris.auth.providers._form import render_login_form`

After the change, `mock.py`'s only remaining csrf-related import need is gone — `CSRF_FORM_FIELD`, `attach_csrf_cookie`, and `mint_csrf_token` are no longer referenced in this file (the helper covers them). Verify with `grep -n "CSRF_FORM_FIELD\|attach_csrf_cookie\|mint_csrf_token" src/iris/auth/providers/mock.py` — must return zero matches before committing.

- [ ] **Step 3: Rewrite `LDAPProvider.begin` to use the helper**

In `src/iris/auth/providers/ldap.py`, replace the `begin` method (lines 37-64) with:

```python
    async def begin(self, request: Request) -> Response:
        return render_login_form(
            request,
            {
                "invalid_credentials": "Invalid username or password.",
                "ldap_unreachable": "Authentication service unreachable. Please try again.",
                "ldap_groups": "Could not load your group membership. Please contact an admin.",
                "csrf_mismatch": "Session expired, please reload and try again.",
            },
        )
```

Then update the imports at the top of `ldap.py`:
- Remove: `from iris.auth.csrf import CSRF_FORM_FIELD, attach_csrf_cookie, mint_csrf_token`
- Add: `from iris.auth.providers._form import render_login_form`

Verify with `grep -n "CSRF_FORM_FIELD\|attach_csrf_cookie\|mint_csrf_token" src/iris/auth/providers/ldap.py` — must return zero matches before committing.

- [ ] **Step 4: Run provider-specific tests**

Run: `uv run pytest tests/auth/test_provider_mock.py tests/auth/test_provider_ldap.py tests/auth/test_login_mock.py tests/auth/test_csrf.py -v`
Expected: all tests pass. These cover both providers' `begin()` flows (form rendering, CSRF cookie attachment, error-message resolution for known and unknown tokens).

- [ ] **Step 5: Run full suite**

Run: `uv run pytest -q`
Expected: `119 passed, 1 skipped, 2 warnings`.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/providers/_form.py src/iris/auth/providers/mock.py src/iris/auth/providers/ldap.py
git commit -m "refactor(providers): extract shared login-form rendering helper"
```

---

## Task 6: Extract `_finalize_login_redirect` helper in routes

**Files:**
- Modify: `src/iris/auth/routes.py:62-142`

**What and why:** `login_post` and `login_callback` both end with the same five steps after a successful authentication: create session, log it, build a 302 redirect to the safe next URL, set the session cookie, delete the CSRF cookie. Extract a helper that takes `(user, target_url, method)` and returns the response. The callback's additional `response.delete_cookie(OAUTH_STATE_COOKIE)` step stays at the call site (it's callback-specific).

**Behavior contract:**
- Identical session creation: `await store.create(user)`.
- Identical log line shape: `"auth: login user=%s subject=%s method=%s groups=%s"` with method `"form"` (POST /login) or `"oauth"` (callback).
- Identical 302 RedirectResponse to `safe_next`.
- Identical session cookie via `_set_session_cookie` with the same kwargs.
- Identical `delete_csrf_cookie(response)` call.
- The callback still adds `response.delete_cookie(OAUTH_STATE_COOKIE)` AFTER the helper returns — order matters for cookie ordering on the response (today: session cookie set, then csrf delete, then oauth_state delete; preserve).
- `_set_session_cookie` is unchanged.

- [ ] **Step 1: Add the helper inside `build_auth_router`**

`build_auth_router` is the closure that holds `store`, `cookie_name`, `cookie_secure`, `ttl_seconds`. The helper needs all four — keeping it as a nested function inside `build_auth_router` (above the route definitions) avoids re-threading config through arguments.

After `login_bucket = TokenBucket(...)` (line 56) and BEFORE the `@router.get("/login")` decorator, insert:

```python
    async def _finalize_login_redirect(
        *, user, target: str, method: str
    ) -> RedirectResponse:
        session = await store.create(user)
        logger.info(
            "auth: login user=%s subject=%s method=%s groups=%s",
            user.display_name,
            user.subject,
            method,
            list(user.groups),
        )
        response = RedirectResponse(target, status_code=302)
        _set_session_cookie(
            response,
            name=cookie_name,
            sid=session.id,
            ttl=ttl_seconds,
            secure=cookie_secure,
        )
        delete_csrf_cookie(response)
        return response
```

Note: today there are TWO log lines with subtly different shapes:
- `login_post`: `"auth: login user=%s subject=%s method=form groups=%s"` (method literal in format string)
- `login_callback`: `"auth: login user=%s subject=%s method=oauth groups=%s"` (method literal in format string)

The helper changes both to `"... method=%s ..."` with `method` as a `%s` argument. The rendered output is byte-identical (`method=form` / `method=oauth`). **If tests pin the format string** (e.g. via `caplog.records[0].msg`) rather than the rendered message, the assertion may break. Check before relying on this:

Run: `grep -n "caplog\|method=form\|method=oauth\|login user=" tests/`
- If any test inspects `record.msg` (the format string itself), this change is observable — STOP and ask.
- If tests inspect `record.getMessage()` (the rendered string) or assert on substrings of the rendered output, no change.

- [ ] **Step 2: Replace the success block in `login_post`**

In `login_post`, replace lines 98-114 (from `session = await store.create(user)` through `delete_csrf_cookie(response)` and the `return response`) with:

```python
        return await _finalize_login_redirect(user=user, target=safe_next, method="form")
```

- [ ] **Step 3: Replace the success block in `login_callback`**

In `login_callback`, replace lines 124-141 (from `session = await store.create(user)` through `delete_csrf_cookie(response)`, but NOT including `response.delete_cookie(OAUTH_STATE_COOKIE)`) with:

```python
        response = await _finalize_login_redirect(user=user, target=safe_next, method="oauth")
        response.delete_cookie(OAUTH_STATE_COOKIE)
        return response
```

The `safe_next = _safe_next(next_url)` line stays in `login_callback` — it's computed before the helper call.

- [ ] **Step 4: Run route- and login-related tests**

Run: `uv run pytest tests/auth/test_login_mock.py tests/auth/test_logout.py tests/auth/test_session_store.py tests/auth/test_provider_oauth.py tests/auth/test_rate_limit.py -v`
Expected: all pass. These cover the full login → session-creation → cookie-setting flow for both form-based providers and the OAuth callback path.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest -q`
Expected: `119 passed, 1 skipped, 2 warnings`.

- [ ] **Step 6: Commit**

```bash
git add src/iris/auth/routes.py
git commit -m "refactor(routes): extract _finalize_login_redirect helper"
```

---

## Task 7: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Re-run full suite, fresh**

Run: `uv run pytest -q`
Expected: `119 passed, 1 skipped, 2 warnings` — identical to the baseline from Task 1.

- [ ] **Step 2: Diff stat against the pre-refactor commit**

Run: `git diff --stat <baseline-commit>..HEAD`
Expected: net negative or near-zero LOC across `src/iris/auth/`. The OAuth file should shrink. The new `_form.py` file should be small (~25 lines). No test files should appear in the diff.

- [ ] **Step 3: Manual smoke check (optional, only if dev server is available)**

Run (in one terminal): `uv run iris`
Run (in another): `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/login` — expect `200` (login form rendered).

If oauth-mode is configured in `.env`, also: `curl -sI http://127.0.0.1:8000/login | grep -i location` — expect a 302 to the IdP. Skip if not configured locally.

- [ ] **Step 4: Summary of residual issues to surface to the user**

Open the post-plan summary message. Include:
1. `OAuthProvider.jwks_uri` property is unused outside the class — flagging for removal approval; not removed per "don't change public interface".
2. (If Task 3 traceback-frame check found anything ambiguous) Note the call-site shift in the `OAuth code exchange failed` log line.
3. (If Task 6 caplog check found anything ambiguous) Note the format-string change in the login log lines.

---

## Self-Review

**Spec coverage:**

- ✅ Remove dead code: confirmed nothing safely-removable was found (`issue_csrf_token`, `jwks_uri` are tested or might be public; flagged the latter).
- ✅ Reduce redundant logic: Tasks 4, 5, 6 (None-checks, login form, login finalization).
- ✅ Replace convoluted constructs: Task 3 (OAuth try/except sentinel).
- ✅ Use descriptive names: helper names `_finalize_login_redirect`, `_request_tokens`, `_verify_id_token`, `_fetch_userinfo`, `_user_from_claims`, `_required_session`, `render_login_form` are all action+object.
- ✅ Reduce nesting: Task 3 takes `exchange_code` from 3 levels of nesting (outer try → inner try → if not id_token) to flat helpers.
- ✅ Break large functions: `exchange_code` (54 lines → 10-line orchestrator + four helpers).
- ✅ Eliminate duplication (DRY): Tasks 2, 4, 5, 6 each remove a real duplication.
- ✅ Don't over-abstract: skipped 5 candidates (listed under "Scope decisions") that would have been style-only or added structure for marginal gain.
- ✅ Modern Python idioms: kept existing style (frozen dataclasses, slots, `Annotated[X, Depends(...)]`, `from __future__ import annotations`); did not add type-narrowing like `match` or `TypeGuard` where the existing pattern reads cleanly.

**Public interface preserved:**
- All `__init__.py` exports unchanged.
- All FastAPI dep aliases (`CurrentUser`, `OptionalCurrentUser`, `CurrentSession`, `SessionData`) keep names + types.
- All four `OAuthProvider` properties retained.
- All `AuthError` token strings retained.
- All log messages retained verbatim (with one flagged format-string structural change in Task 6).
- All HTTP status codes, redirect targets, cookie attributes retained.

**Placeholder scan:**
- No "TBD", "implement later", "add appropriate error handling", "similar to Task N", or "fill in details" in any task. Every code step shows the exact code.
- Every test step shows the exact `uv run pytest ...` command and expected outcome.
- Every commit step shows the exact `git add` + `git commit` command.

**Type consistency:**
- `_required_session(session: _ResolvedSession) -> UserSession` — `_ResolvedSession` is the existing alias from line 47; `UserSession` is the existing dataclass from `iris.auth.identity`. ✓
- `_RequiredSession = Annotated[UserSession, Depends(_required_session)]` — new alias, used only in `deps.py`, not exported. ✓
- `_finalize_login_redirect(*, user, target: str, method: str) -> RedirectResponse` — `user` is intentionally untyped (it's `User` from `iris.auth.identity` but importing it just to annotate this nested function adds no value); reads as a private closure. **Flag:** if the executor wants stricter typing, add `from iris.auth.identity import User` and annotate `user: User`. Not required for behavior preservation.
- `render_login_form(request: Request, error_messages: dict[str, str]) -> Response` — `Request` and `Response` from `fastapi`; `dict[str, str]` is the type of the per-provider error tables. ✓
- `_request_tokens` returns `dict`, `_fetch_userinfo` returns `dict`, `_user_from_claims` takes `dict` — consistent with how the original `exchange_code` body treats these blobs (no Pydantic model in sight today). ✓

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-03-simplify-iris-codebase.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
