# Security hardening — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

Second of three follow-up specs from the 2026-05-09 review. The auth-module-reshape spec landed first (commit `b11cf20`), so file/symbol names below assume the post-reshape layout (`Capabilities`, `iris.auth.store`, `iris.auth.views`, `iris.clickhouse.capabilities`).

This spec closes seven security findings from the review as one cohesive pass:

- **S1** — `TokenBucket` (in `iris.auth.rate_limit`) has unbounded memory growth: each unique rate-limit key creates a permanent dict entry. An attacker spraying `POST /login` from rotating IPs slowly exhausts memory; the rate limiter itself becomes the DoS surface.
- **S2** — `request.client.host` is the proxy's IP behind a load balancer, so the rate-limit bucket becomes effectively global (one user can DoS the whole instance) and audit logs lose attribution. The deployment workaround (`uvicorn --proxy-headers --forwarded-allow-ips=...`) is documented in `docs/operations.md` but easy to forget.
- **S3** — `src/iris/templates/base.html` loads Datastar from `cdn.jsdelivr.net` over plain `<script src=...>` with no Subresource Integrity hash. A CDN compromise (or repo cache poisoning) would deliver attacker-controlled JS to every authenticated page.
- **S4** — `OAuthProvider.begin` sets the `oauth_state` cookie without an explicit `path=` attribute. The browser default (the directory of `/login`) happens to match `/login/callback` today; any future route move silently breaks login.
- **S5** — `verify_csrf_form` only reads CSRF tokens from form bodies. Future Datastar `@post('/url')` routes (which CLAUDE.md already documents) send JSON bodies and would silently bypass the CSRF gate.
- **S8** — `_safe_next` checks for `\\` and absolute URLs but not `\r` / `\n`. CRLF in `next` could (depending on response framework) split into a Location header that injects another header.
- **U5** — `_safe_next` returns `/` silently for any rejected input. An operator debugging a misconfigured client gets no log line.

The seven items share a code surface (auth routes, CSRF helpers, middleware, configuration) and benefit from one review window. Single PR.

## Goal

Close S1, S2, S3, S4, S5, S8, and U5 with surgical changes. Add tests for each new behavior. Document operator-facing configuration changes in `docs/operations.md`.

## Non-goals (deferred to the third spec)

- SQL/identifier hygiene: database-name suffix validation, `_FIXED_STRING_RE` dedup (B5), escape-grammar unification (B6), `delete_database` orphan-grant sweep (U4).
- Anything else from the review.

## Atomicity

Single commit on a feature branch. `uv run ruff check`, `uv run basedpyright --level warning`, and the full pytest suite (including integration) must be green before merge.

---

## 1. File touch list

```
NEW:
  src/iris/auth/client_ip.py           # client_ip(request, *, trust_forwarded) (S2)
  src/iris/static/datastar.js          # vendored Datastar bundle (S3)
  tests/auth/test_client_ip.py         # client_ip helper tests (S2)
  tests/test_static_assets.py          # /static/datastar.js served correctly (S3)

MODIFIED:
  src/iris/app.py                      # mount StaticFiles at /static (S3)
  src/iris/auth/config.py              # +trust_forwarded_for: bool (S2)
  src/iris/auth/csrf.py                # +verify_csrf_header (S5)
  src/iris/auth/deps.py                # set_settings propagates trust_forwarded_for (S2)
  src/iris/auth/rate_limit.py          # bounded LRU TokenBucket (S1)
  src/iris/auth/routes.py              # use client_ip; CRLF + info log in _safe_next (S2, S8, U5)
  src/iris/auth/providers/oauth.py     # explicit path="/" on state cookie (S4)
  src/iris/middleware.py               # drop jsdelivr from CSP (S3)
  src/iris/templates/base.html         # /static/datastar.js (S3)
  pyproject.toml                       # ensure iris/static/*.js ships in the wheel
  docs/operations.md                   # IRIS_TRUST_FORWARDED_FOR; note vendored Datastar
  tests/auth/test_rate_limit.py        # add LRU-eviction cases (S1)
  tests/auth/test_csrf.py              # add header-dep cases (S5)
  tests/auth/test_login_method_not_allowed.py  # _safe_next CRLF + logging (S8, U5)
  tests/test_security_headers.py       # CSP no longer mentions jsdelivr (S3)
```

---

## 2. Component-by-component design

### 2.1 — S1: bounded TokenBucket

`src/iris/auth/rate_limit.py`. Replace the `defaultdict[str, tuple[float, float]]` with an `OrderedDict[str, tuple[float, float]]` plus a module constant `_MAX_BUCKETS = 10_000`.

**On `take(key)`:**

- If `key` is in the dict: refill / consume tokens as today; then `_buckets.move_to_end(key)` to mark it most-recently-used.
- If `key` is not in the dict: insert a fresh `(capacity, now)` entry. After insert, if `len(_buckets) > _MAX_BUCKETS`, call `_buckets.popitem(last=False)` to drop the LRU entry.

**Memory ceiling:** ~32 bytes per entry × 10K = 0.32 MB. Predictable upper bound regardless of input pattern.

**Behavior preserved:** A returning legitimate client always finds its bucket present (and gets bumped to MRU). Only entries idle for >10K unique-IP-attempts get evicted. An evicted attacker effectively gets a fresh bucket on the next attempt — equivalent to "we forgot you, take 10 free tokens." At that operational scale the rate limiter alone cannot defend; this spec accepts the trade-off in §4 below.

`_MAX_BUCKETS` is a module-level int, not env-configurable (YAGNI).

### 2.2 — S2: `client_ip` helper + `IRIS_TRUST_FORWARDED_FOR`

**New module:** `src/iris/auth/client_ip.py`:

```python
"""Resolve a request's client IP, honoring trusted X-Forwarded-For when configured."""
from __future__ import annotations

from fastapi import Request


def client_ip(request: Request, *, trust_forwarded: bool) -> str:
    """Return the client IP for rate-limiting / audit logging.

    When ``trust_forwarded`` is True and ``X-Forwarded-For`` is non-empty,
    return its leftmost (original-client) entry. Otherwise return
    ``request.client.host`` (or "unknown" if Starlette didn't populate it).

    Per OWASP, the leftmost IP in X-Forwarded-For is the original client;
    subsequent IPs are intermediate proxies. Operators MUST configure their
    trusted proxy to strip any client-supplied X-Forwarded-For before adding
    its own — otherwise an attacker can spoof the leftmost value.
    """
    if trust_forwarded:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return first
    return request.client.host if request.client else "unknown"
```

**Configuration:** `AuthSettings.from_env()` gains:

```python
trust_forwarded_for: bool

# in from_env():
trust_forwarded_for=_get_bool("IRIS_TRUST_FORWARDED_FOR", False),
```

`set_settings()` in `iris.auth.deps` accepts and stores `trust_forwarded_for` on `app.state.trust_forwarded_for`. `iris.auth.routes.install` reads `settings.trust_forwarded_for` and forwards it to `set_settings`.

**Call sites in `routes.py`:**

```python
# was:
client_host = request.client.host if request.client else "unknown"

# becomes:
from iris.auth.client_ip import client_ip
client_host = client_ip(request, trust_forwarded=request.app.state.trust_forwarded_for)
```

Two call sites: rate-limit `take()` key and the audit-log `remote_addr` field.

### 2.3 — S3: vendored Datastar bundle

**Vendor location:** `src/iris/static/datastar.js`. The file is downloaded once during implementation from `https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js` and committed verbatim. Approximate size: 48 KB minified.

**StaticFiles mount in `src/iris/app.py`:**

```python
from pathlib import Path
from fastapi.staticfiles import StaticFiles

# in build_app(), after middleware installation:
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)
```

The mount is unconditional (regardless of `install_clickhouse`) — the static asset is needed by the index page in both modes.

**Template change:** `src/iris/templates/base.html`:

```html
<!-- was -->
<script type="module" src="https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"></script>

<!-- becomes -->
<script type="module" src="/static/datastar.js"></script>
```

**CSP update in `src/iris/middleware.py`:** Drop `https://cdn.jsdelivr.net` from `script-src`. The directive becomes `script-src 'self' 'unsafe-eval'`. Update the comment block above `_CSP` to remove the CDN paragraph.

**Wheel packaging:** Hatchling's default behavior includes data files alongside Python sources. The implementation plan verifies inclusion via `uv build && unzip -l dist/iris-0.1.0-*.whl | grep static/datastar.js` — if the file is absent, add `[tool.hatch.build.targets.wheel.force-include]` with `"src/iris/static/datastar.js" = "iris/static/datastar.js"`.

**Documentation:** `docs/operations.md` gets a one-paragraph note explaining that Datastar is vendored, where it's served from, and that bumping the version requires re-downloading and committing the new file (no SRI re-hash to manage; same-origin so SRI is redundant).

### 2.4 — S4: OAuth state cookie path

`src/iris/auth/providers/oauth.py:137-144`. Add `path="/"` to the `set_cookie(...)` call:

```python
response.set_cookie(
    OAUTH_STATE_COOKIE,
    signed,
    max_age=STATE_COOKIE_TTL,
    httponly=True,
    secure=secure,
    samesite="lax",
    path="/",            # NEW
)
```

One line. No new test (existing OAuth integration tests would fail if the cookie weren't sent on `/login/callback`).

### 2.5 — S5: `verify_csrf_header`

`src/iris/auth/csrf.py`. Add a sibling sync function next to `verify_csrf_form`:

```python
def verify_csrf_header(request: Request) -> None:
    """Verify CSRF for non-form requests via the X-CSRF-Token header.

    Use ``Depends(verify_csrf_header)`` on JSON / Datastar @post / @put / @patch
    / @delete routes. The token transmission is still double-submit:
    server compares the cookie value with the header value via
    ``hmac.compare_digest``. Client-side code reads ``CSRF_COOKIE_NAME``
    (which is JS-readable) and copies it into the request header.
    """
    cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=400, detail="csrf_mismatch")
```

`verify_csrf_form` is unchanged (sync, form-only). The module docstring gains a one-paragraph "which dep to pick" guide so future route authors don't guess.

### 2.6 — S8 + U5: `_safe_next` CRLF guard + info logging

`src/iris/auth/routes.py:38-46`. Replace the function:

```python
def _safe_next(next_url: str) -> str:
    """Return next_url only if it's a same-origin relative path; else /.

    Defends against open-redirect attacks via a crafted `next` query param.
    Rejects: empty input, CRLF (header injection), backslash (browser
    normalization), absolute URLs, protocol-relative URLs (`//evil`).
    Logs every rewrite at INFO so operators tracing client misconfiguration
    or attempted attacks can see the rejected value (truncated to 128 chars
    to defend against log-injection).
    """
    if not next_url:
        return "/"
    if "\r" in next_url or "\n" in next_url:
        logger.info("auth: safe_next_rejected reason=crlf next=%r", next_url[:128])
        return "/"
    if "\\" in next_url:
        logger.info("auth: safe_next_rejected reason=backslash next=%r", next_url[:128])
        return "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        logger.info("auth: safe_next_rejected reason=non_relative next=%r", next_url[:128])
        return "/"
    return next_url
```

Combines S8 (CRLF check before existing logic — earliest rejection point) and U5 (info logging on every rejection branch). Empty-string input rewrites silently with no log because that's the natural default for a missing query param, not a misconfiguration signal.

---

## 3. Migration & deployment

### Operator-facing changes

**`IRIS_TRUST_FORWARDED_FOR`** — new boolean env var. Default `false`. Set to `true` when iris runs behind a trusted reverse proxy that adds X-Forwarded-For. Operators who already use `uvicorn --proxy-headers` will see no behavioral difference (`request.client.host` already reflects X-Forwarded-For under that flag); the new env var just adds an app-level toggle for deployments where the operator prefers to leave uvicorn's CLI flags untouched.

**Trusted-proxy requirement** — when `IRIS_TRUST_FORWARDED_FOR=true`, the upstream proxy MUST strip any client-supplied X-Forwarded-For before adding its own. Documented in `docs/operations.md`.

**Vendored Datastar** — operators who blocklist `cdn.jsdelivr.net` at the edge no longer need an exception; iris serves the script from `/static/datastar.js` at the same origin. Bumping the Datastar version is a one-step process: re-download `https://cdn.jsdelivr.net/gh/starfederation/datastar@<version>/bundles/datastar.js` over `src/iris/static/datastar.js` and commit. No SRI hash to refresh.

**No SQLite migration.** This spec doesn't touch the session-store schema.

### `docs/operations.md` updates

A short paragraph under each of:

- **Env vars** table — add `IRIS_TRUST_FORWARDED_FOR` row.
- **Multi-worker deployment** — note that `IRIS_TRUST_FORWARDED_FOR=true` with a misconfigured proxy lets clients spoof their IP; recommend pairing with a proxy-side strip.
- **Open security follow-ups** — replace the existing rate-limiting bullet with a note that S1 (eviction) and S2 (X-Forwarded-For) are now closed.
- **Deferred** — add a "Datastar version refresh" line.

---

## 4. Risk acceptance

What this spec does NOT close — explicit so the next reviewer knows:

- **Rate-limit eviction is best-effort under attack.** A 10K-LRU cap means an attacker controlling >10K unique IPs evicts legitimate users' buckets, giving themselves fresh capacity per IP rotation. Acceptable for ≤20-user single-host deployments; a real DDoS demands an upstream WAF.
- **`client_ip` trusts X-Forwarded-For literally when the env flag is on.** If the trusted proxy doesn't strip a client-supplied header, the leftmost IP could be attacker-controlled. Mitigation is operator discipline, not code.
- **The vendored Datastar bundle ages.** Upgrading Datastar requires re-downloading the file and committing it. There is no automated check that the vendored file matches a known-good upstream hash. Acceptable at the v0.1.0 cadence.
- **`verify_csrf_header` does not protect against XSS.** A successful XSS on the iris origin can read the cookie and forge the header. CSRF protection is meaningful only against cross-site forgery without script execution on the iris origin. Defense-in-depth: the cookie remains `samesite="lax"`.

---

## 5. Testing strategy

This is the first spec in the series that adds new behavior, so each fix gets at least one new test. Existing tests (376 unit + 23 integration as of `b11cf20`) must stay green.

### 5.1 — S1: TokenBucket eviction

`tests/auth/test_rate_limit.py`. Three new tests:

- `test_lru_evicts_oldest_when_capacity_exceeded` — insert 10001 distinct keys; assert key #0 is gone, key #10000 is present, len is 10000.
- `test_returning_key_is_promoted_to_mru` — insert 10000 keys, take() the first key (promoting it), insert one more key. Assert key #1 (not #0) was evicted.
- `test_evicted_key_starts_with_full_bucket_on_re_insert` — insert key, drain its bucket, force eviction by spamming 10000 other keys, re-insert original key, assert it has full capacity.

The `_MAX_BUCKETS` constant is read by tests via `from iris.auth.rate_limit import _MAX_BUCKETS` (no env override, no monkeypatching).

### 5.2 — S2: `client_ip` helper

`tests/auth/test_client_ip.py` (new). Cases (each builds a fake `Request` via Starlette's `Request(scope, receive)` constructor):

- `trust_forwarded=False`, no header → `request.client.host`.
- `trust_forwarded=False`, header present → `request.client.host` (header ignored).
- `trust_forwarded=True`, header missing → falls back to `request.client.host`.
- `trust_forwarded=True`, single IP `"1.2.3.4"` → `"1.2.3.4"`.
- `trust_forwarded=True`, comma list `"1.2.3.4, 5.6.7.8"` → `"1.2.3.4"`.
- `trust_forwarded=True`, leading whitespace `" 1.2.3.4"` → `"1.2.3.4"`.
- `trust_forwarded=True`, empty string → falls back.
- `trust_forwarded=True`, `request.client is None` → `"unknown"` (degenerate fallback chain).

Plus one route-level integration test added to `tests/auth/test_rate_limit.py` that confirms the rate-limit bucket key uses the X-Forwarded-For first IP when `IRIS_TRUST_FORWARDED_FOR=true`.

### 5.3 — S3: vendored static asset + CSP update

- `tests/test_static_assets.py` (new) — `TestClient` GET `/static/datastar.js`, assert 200, content-type starts with `application/javascript` or `text/javascript`, body length > 10 KB (sanity check that the file is the real bundle, not a stub), body decodes as valid UTF-8 (the bundle is plain JS source, not a binary).
- `tests/test_security_headers.py` — extend the existing CSP test: assert `"cdn.jsdelivr.net"` does NOT appear anywhere in the CSP header; assert `script-src 'self' 'unsafe-eval'` IS present (no third-party origin).

### 5.4 — S4: OAuth state cookie path

No new test. The existing OAuth integration tests (`tests/auth/integration/test_oauth_integration.py`) drive `/login` → `/login/callback` end-to-end and would fail if the cookie weren't sent on the callback path.

### 5.5 — S5: `verify_csrf_header`

`tests/auth/test_csrf.py`. Four new tests, mirroring the existing `verify_csrf_form` shape:

- `test_verify_csrf_header_passes_when_cookie_matches_header` — cookie + matching `X-CSRF-Token` header → no exception.
- `test_verify_csrf_header_rejects_missing_cookie` — header present, cookie absent → 400.
- `test_verify_csrf_header_rejects_missing_header` — cookie present, header absent → 400.
- `test_verify_csrf_header_rejects_mismatch` — cookie + different header value → 400.

### 5.6 — S8 + U5: `_safe_next` CRLF + logging

`tests/auth/test_login_method_not_allowed.py` (or whichever existing file covers the redirect path) gets:

- `test_safe_next_rejects_crlf_next_param` — POST `/login` with `next=/foo%0d%0aSet-Cookie:%20x=y` (URL-encoded CRLF). Assert the redirect Location is `/`, not the malicious value.
- `test_safe_next_logs_info_on_rejection` — pytest `caplog` captures the INFO log line with the truncated `next` value and the `reason=` field.

### 5.7 — Gates

1. `uv run ruff check` — zero warnings.
2. `uv run basedpyright --level error` — zero errors.
3. `uv run basedpyright --level warning` — zero warnings.
4. `uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration` — green; total count = pre-spec baseline + new tests.
5. `uv run pytest tests/auth/integration` — green (Docker required).
6. `uv run pytest tests/clickhouse/integration` — green (Docker required).

---

## 6. Out of scope

Reserved for the third spec (SQL/identifier hygiene); MUST NOT be touched in this commit:

- Database-name suffix-block validation (the `_DBADMIN`/`_USER` collision).
- `_FIXED_STRING_RE` deduplication (B5).
- `quote_string` vs `_marshal_array_element` escape unification (B6).
- `delete_database` orphan-grant sweep (U4).

Plus everything else from the original review that the user did not include in the prioritized fix list (rate-limit-key salting, OAuth nonce sliding window, etc.). If anyone is tempted while doing the security work, leave it.
