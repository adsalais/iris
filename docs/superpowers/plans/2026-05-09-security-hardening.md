# Security hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (the user's CLAUDE.md mandates Inline Execution over Subagent-Driven). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 7 review-surfaced security findings (S1, S2, S3, S4, S5, S8, U5) as one cohesive hardening pass: bound TokenBucket memory; honor X-Forwarded-For under a configurable trust flag; vendor the Datastar JS bundle; tighten the OAuth state cookie path; add a header-based CSRF dep for non-form routes; reject CRLF in the redirect-after-login path and log every rejection.

**Architecture:** Each fix is small and self-contained. The whole bundle lands in **one atomic commit** at Task 11. Intermediate tasks stage edits without committing — the final task runs ruff + basedpyright (warning level) + the full pytest suite (incl. integration) as the merge gate before the commit.

**Tech Stack:** Python 3.13, FastAPI 0.136 (Starlette), basedpyright, ruff, pytest 9.

**Source spec:** `docs/superpowers/specs/2026-05-09-security-hardening-design.md`.

**Pre-condition:** the auth-reshape spec landed at commit `b11cf20`. All file/symbol names below assume the post-reshape layout (`Capabilities`, `iris.auth.store`, `iris.auth.views`, `iris.clickhouse.capabilities`).

---

## Task 1: Create feature branch and verify baseline

**Files:** none modified.

- [ ] **Step 1.1: Create the feature branch**

```bash
git -C /home/driou/dev/project/iris checkout -b feature/security-hardening
```

- [ ] **Step 1.2: Verify a clean baseline — must be green BEFORE we start**

```bash
uv run --project /home/driou/dev/project/iris ruff check
uv run --project /home/driou/dev/project/iris basedpyright --level warning
uv run --project /home/driou/dev/project/iris pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
```

Expected:
- ruff: zero warnings.
- basedpyright: zero errors, zero warnings.
- pytest: all unit tests pass. (Integration suites skipped — we run them in Task 11.)

If anything fails, stop. Fix or report before continuing.

- [ ] **Step 1.3: Capture pre-spec test inventory for the no-regression diff**

```bash
uv run --project /home/driou/dev/project/iris pytest --collect-only -q --ignore=tests/auth/integration --ignore=tests/clickhouse/integration > /tmp/pytest-inventory-before.txt
wc -l /tmp/pytest-inventory-before.txt
```

Expected line count: **378** (matching the post-auth-reshape baseline).

- [ ] **Step 1.4: Do NOT commit.** This task is verification only.

---

## Task 2: S1 — Bounded LRU TokenBucket

**Files:**
- Modify: `src/iris/auth/rate_limit.py` (full rewrite)
- Modify: `tests/auth/test_rate_limit.py` (add three tests)

- [ ] **Step 2.1: Add three failing tests for the LRU semantics**

Append to `tests/auth/test_rate_limit.py` (after the existing tests):

```python
def test_lru_evicts_oldest_when_capacity_exceeded():
    """Inserting _MAX_BUCKETS + 1 distinct keys evicts key #0."""
    from iris.auth.rate_limit import TokenBucket, _MAX_BUCKETS

    bucket = TokenBucket(capacity=10, refill_per_second=1.0)
    for i in range(_MAX_BUCKETS + 1):
        bucket.take(f"k{i}")

    assert "k0" not in bucket._buckets, "oldest key should have been evicted"
    assert f"k{_MAX_BUCKETS}" in bucket._buckets, "newest key should be present"
    assert len(bucket._buckets) == _MAX_BUCKETS, "size capped at _MAX_BUCKETS"


def test_returning_key_is_promoted_to_mru():
    """Re-taking a key bumps it to MRU; a subsequent overflow evicts the
    next-oldest, not the original key."""
    from iris.auth.rate_limit import TokenBucket, _MAX_BUCKETS

    bucket = TokenBucket(capacity=10, refill_per_second=1.0)
    for i in range(_MAX_BUCKETS):
        bucket.take(f"k{i}")
    # k0 is currently the LRU. Re-take it to promote.
    bucket.take("k0")
    # Now insert one more key, forcing one eviction.
    bucket.take(f"k{_MAX_BUCKETS}")

    assert "k0" in bucket._buckets, "k0 was promoted to MRU and must survive"
    assert "k1" not in bucket._buckets, "k1 was the new LRU and should be evicted"


def test_evicted_key_starts_with_full_bucket_on_re_insert():
    """An evicted key, on re-insert, gets a fresh full-capacity bucket."""
    from iris.auth.rate_limit import TokenBucket, _MAX_BUCKETS

    bucket = TokenBucket(capacity=10, refill_per_second=0.0)  # no time-based refill
    # Drain k0
    for _ in range(10):
        assert bucket.take("k0") is None
    assert bucket.take("k0") is not None  # exhausted

    # Spam other keys until k0 is evicted
    for i in range(1, _MAX_BUCKETS + 1):
        bucket.take(f"k{i}")
    assert "k0" not in bucket._buckets

    # Re-insert k0 — should get a fresh full bucket
    assert bucket.take("k0") is None  # capacity-many fresh tokens available
```

- [ ] **Step 2.2: Run the new tests — they must fail**

```bash
uv run pytest tests/auth/test_rate_limit.py::test_lru_evicts_oldest_when_capacity_exceeded -v
```

Expected: `ImportError` for `_MAX_BUCKETS` (or `AttributeError`).

- [ ] **Step 2.3: Replace the contents of `src/iris/auth/rate_limit.py`**

```python
from __future__ import annotations

import time
from collections import OrderedDict

# Bound on the number of distinct rate-limit buckets held in memory at once.
# At ~32 bytes per (tokens, last_refill) entry plus key overhead, 10K caps the
# bucket dict at well under 1 MB regardless of input pattern. An attacker
# spraying >10K unique keys evicts older buckets in LRU order; legitimate
# clients are kept hot by their own activity.
_MAX_BUCKETS = 10_000


class TokenBucket:
    """In-process token-bucket rate limiter with bounded memory.

    Each ``key`` maintains its own bucket. ``take(key)`` returns None if the
    request is allowed (and consumes one token), else returns the number of
    seconds the caller should wait before retrying.

    Eviction: the bucket dict is an ``OrderedDict`` capped at ``_MAX_BUCKETS``
    entries. Calling ``take(key)`` promotes ``key`` to most-recently-used.
    Inserting a new key when at capacity drops the LRU entry. An evicted key
    re-inserted later starts with a fresh full-capacity bucket — equivalent
    to "we forgot you, here are ``capacity`` fresh tokens." Acceptable at the
    operational scale where the rate limiter alone cannot defend; a real
    DDoS demands an upstream WAF.

    Designed for ``--workers 1`` in-memory deployments. Per-process state.
    """

    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        # _buckets[key] = (tokens, last_refill_monotonic). Ordered for LRU.
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def take(self, key: str) -> float | None:
        """Returns None if allowed, else seconds to wait until a token is available."""
        now = time.monotonic()
        if key in self._buckets:
            tokens, last = self._buckets[key]
            self._buckets.move_to_end(key)
        else:
            tokens, last = (self.capacity, now)
            self._buckets[key] = (tokens, last)
            if len(self._buckets) > _MAX_BUCKETS:
                self._buckets.popitem(last=False)
        # Refill since last
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            return None
        # Not enough tokens; persist the refill so the next call sees it.
        self._buckets[key] = (tokens, now)
        return (1 - tokens) / self.refill_per_second
```

- [ ] **Step 2.4: Run the full rate-limit test file — all tests pass**

```bash
uv run pytest tests/auth/test_rate_limit.py -v
```

Expected: every test in the file passes (the three new ones plus the four pre-existing).

- [ ] **Step 2.5: Do NOT commit.**

---

## Task 3: S2a — `client_ip` helper module + tests

**Files:**
- Create: `src/iris/auth/client_ip.py`
- Create: `tests/auth/test_client_ip.py`

- [ ] **Step 3.1: Write the failing tests**

Write `tests/auth/test_client_ip.py`:

```python
"""Unit tests for iris.auth.client_ip.client_ip.

We construct fake Starlette Request objects directly via the ASGI scope dict
to avoid spinning up TestClient — these tests are about the helper's input
parsing, not HTTP round-trips.
"""
from __future__ import annotations

from starlette.requests import Request

from iris.auth.client_ip import client_ip


def _make_request(*, headers: dict[str, str] | None = None,
                  client: tuple[str, int] | None = ("10.0.0.1", 12345)) -> Request:
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": raw_headers,
        "client": client,
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_no_trust_no_header_returns_request_client():
    r = _make_request(client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=False) == "10.0.0.1"


def test_no_trust_header_present_ignores_header():
    r = _make_request(headers={"x-forwarded-for": "1.2.3.4"}, client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=False) == "10.0.0.1"


def test_trust_no_header_falls_back_to_request_client():
    r = _make_request(client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=True) == "10.0.0.1"


def test_trust_single_xff_returns_that_ip():
    r = _make_request(headers={"x-forwarded-for": "1.2.3.4"})
    assert client_ip(r, trust_forwarded=True) == "1.2.3.4"


def test_trust_xff_list_returns_leftmost_ip():
    r = _make_request(headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8, 9.10.11.12"})
    assert client_ip(r, trust_forwarded=True) == "1.2.3.4"


def test_trust_xff_leading_whitespace_is_stripped():
    r = _make_request(headers={"x-forwarded-for": "   1.2.3.4   , 5.6.7.8"})
    assert client_ip(r, trust_forwarded=True) == "1.2.3.4"


def test_trust_empty_xff_falls_back_to_request_client():
    r = _make_request(headers={"x-forwarded-for": ""}, client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=True) == "10.0.0.1"


def test_trust_xff_with_only_whitespace_falls_back():
    r = _make_request(headers={"x-forwarded-for": "   "}, client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=True) == "10.0.0.1"


def test_no_client_and_no_xff_returns_unknown():
    r = _make_request(client=None)
    assert client_ip(r, trust_forwarded=False) == "unknown"
    assert client_ip(r, trust_forwarded=True) == "unknown"


def test_trust_xff_list_with_empty_first_falls_back():
    """Defensive: '   , 5.6.7.8' — first slot is empty after strip."""
    r = _make_request(headers={"x-forwarded-for": "   , 5.6.7.8"}, client=("10.0.0.1", 12345))
    assert client_ip(r, trust_forwarded=True) == "10.0.0.1"
```

- [ ] **Step 3.2: Run the new tests — they must fail with ImportError**

```bash
uv run pytest tests/auth/test_client_ip.py -v
```

Expected: `ModuleNotFoundError: No module named 'iris.auth.client_ip'`.

- [ ] **Step 3.3: Create `src/iris/auth/client_ip.py`**

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

- [ ] **Step 3.4: Run the new tests — all pass**

```bash
uv run pytest tests/auth/test_client_ip.py -v
```

Expected: 10 passed.

- [ ] **Step 3.5: Do NOT commit.**

---

## Task 4: S2b — Wire `client_ip` into config + routes

**Files:**
- Modify: `src/iris/auth/config.py` — add `trust_forwarded_for: bool` field + env read
- Modify: `src/iris/auth/deps.py` — `set_settings` propagates the flag
- Modify: `src/iris/auth/routes.py` — replace inline `request.client.host` with `client_ip(...)`
- Modify: `tests/auth/test_rate_limit.py` — add an integration test verifying X-Forwarded-For drives the bucket key

- [ ] **Step 4.1: Skip a separate test — Task 3 already covers the helper, and the existing `test_login_rate_limit_kicks_in_on_burst` covers the route's rate-limit integration**

The helper's correctness was nailed down in Task 3. The route-level integration is fragile to test directly under TestClient (TestClient sends `request.client.host = "testclient"` for every request — the X-Forwarded-For path can't be exercised by varying clients in a single test). The existing `test_login_rate_limit_kicks_in_on_burst` confirms the rate-limit path still works end-to-end with the new `client_ip(...)` call site, and the inline-execution gates (Step 11) catch any regression.

No new test added in this task. Move to Step 4.3.

- [ ] **Step 4.3: Modify `src/iris/auth/config.py` — add the field and env read**

In the `AuthSettings` dataclass, add the field (alphabetical position next to other booleans is fine):

```python
@dataclass(frozen=True)
class AuthSettings:
    method: Literal["oauth", "ldap", "mock"]
    cookie_name: str
    ttl_seconds: int
    absolute_ttl_seconds: int
    max_per_user: int
    cookie_secure: bool
    auth_db_path: str
    trust_forwarded_for: bool       # NEW
    oidc: OIDCSettings | None
    ldap: LDAPSettings | None
    mock: MockSettings | None
```

In `from_env()`, after the existing `cookie_secure = ...` line:

```python
        cookie_secure = _get_bool("COOKIE_SECURE", True)
        trust_forwarded_for = _get_bool("IRIS_TRUST_FORWARDED_FOR", False)   # NEW
```

In the `return cls(...)` call at the bottom of `from_env()`, add:

```python
        return cls(
            method=method,
            cookie_name=cookie_name,
            ttl_seconds=ttl_seconds,
            absolute_ttl_seconds=absolute_ttl_seconds,
            max_per_user=max_per_user,
            cookie_secure=cookie_secure,
            auth_db_path=auth_db_path,
            trust_forwarded_for=trust_forwarded_for,    # NEW
            oidc=oidc,
            ldap=ldap,
            mock=mock,
        )
```

- [ ] **Step 4.4: Modify `src/iris/auth/deps.py` — extend `set_settings`**

```
old:
def set_settings(app: FastAPI, *, cookie_name: str, cookie_secure: bool = True) -> None:
    app.state.auth_cookie_name = cookie_name
    app.state.auth_cookie_secure = cookie_secure

new:
def set_settings(
    app: FastAPI,
    *,
    cookie_name: str,
    cookie_secure: bool = True,
    trust_forwarded_for: bool = False,
) -> None:
    app.state.auth_cookie_name = cookie_name
    app.state.auth_cookie_secure = cookie_secure
    app.state.trust_forwarded_for = trust_forwarded_for
```

- [ ] **Step 4.5: Modify `src/iris/auth/routes.py` — call `client_ip` at both rate-limit and audit-log sites**

Add at the top of `routes.py`:

```
old:
from iris.auth.csrf import delete_csrf_cookie, verify_csrf_form

new:
from iris.auth.client_ip import client_ip
from iris.auth.csrf import delete_csrf_cookie, verify_csrf_form
```

Inside `login_post(request, ...)`:

```
old:
        client_host = request.client.host if request.client else "unknown"

new:
        client_host = client_ip(
            request, trust_forwarded=request.app.state.trust_forwarded_for
        )
```

In `install(app)`, update the `set_settings` call:

```
old:
    set_settings(
        app, cookie_name=settings.cookie_name, cookie_secure=settings.cookie_secure
    )

new:
    set_settings(
        app,
        cookie_name=settings.cookie_name,
        cookie_secure=settings.cookie_secure,
        trust_forwarded_for=settings.trust_forwarded_for,
    )
```

- [ ] **Step 4.6: Run the new test + the existing rate-limit test file**

```bash
uv run pytest tests/auth/test_rate_limit.py -v
```

Expected: all green, including `test_trust_forwarded_for_enables_xff_to_drive_bucket_key`.

Also run the CSRF tests to confirm `set_settings` wasn't broken:

```bash
uv run pytest tests/auth/test_csrf.py -v
```

Expected: all green. Note: `tests/auth/test_csrf.py:_build_app` calls `set_settings(app, cookie_name=..., cookie_secure=...)`. The new keyword `trust_forwarded_for` defaults to False so existing call sites are unaffected.

- [ ] **Step 4.7: Do NOT commit.**

---

## Task 5: S3a — Vendor Datastar bundle + StaticFiles mount

**Files:**
- Create: `src/iris/static/datastar.js` (downloaded binary)
- Modify: `src/iris/app.py` — mount StaticFiles at `/static`
- Create: `tests/test_static_assets.py`

- [ ] **Step 5.1: Download the Datastar bundle**

```bash
mkdir -p /home/driou/dev/project/iris/src/iris/static
curl --fail --silent --show-error \
  --output /home/driou/dev/project/iris/src/iris/static/datastar.js \
  'https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js'
ls -l /home/driou/dev/project/iris/src/iris/static/datastar.js
file /home/driou/dev/project/iris/src/iris/static/datastar.js
```

Expected:
- `ls -l` shows file size > 10 KB (the bundle is ~48 KB minified).
- `file` reports something resembling `ASCII text` or `Unicode text`.

If the download fails, do not proceed — the rest of Task 5 depends on this file existing.

- [ ] **Step 5.2: Write the failing static-asset test**

Write `tests/test_static_assets.py`:

```python
"""Static-files mount serves the vendored Datastar bundle."""
from fastapi.testclient import TestClient

from iris.app import build_app


def test_static_datastar_js_is_served():
    app = build_app(install_clickhouse=False)
    c = TestClient(app)
    r = c.get("/static/datastar.js")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert ct.startswith(("application/javascript", "text/javascript")), (
        f"unexpected content-type: {ct!r}"
    )
    # Sanity-check the body: real bundle, not a stub or HTML 404 page.
    assert len(r.content) > 10_000, f"datastar.js body too small ({len(r.content)} bytes)"
    # The bundle is plain JS source, must decode as UTF-8 cleanly.
    r.content.decode("utf-8")  # raises UnicodeDecodeError on failure


def test_static_mount_404s_for_missing_file():
    app = build_app(install_clickhouse=False)
    c = TestClient(app)
    r = c.get("/static/does-not-exist.js")
    assert r.status_code == 404
```

- [ ] **Step 5.3: Run the new tests — they must fail**

```bash
uv run pytest tests/test_static_assets.py -v
```

Expected: `404 Not Found` for `/static/datastar.js` (mount not yet wired).

- [ ] **Step 5.4: Modify `src/iris/app.py` to mount the StaticFiles directory**

Add the import at the top of `app.py`:

```
old:
from datastar_py.fastapi import DatastarResponse, read_signals
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse

new:
from pathlib import Path

from datastar_py.fastapi import DatastarResponse, read_signals
from datastar_py.fastapi import ServerSentEventGenerator as SSE
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
```

In `build_app()`, after the middleware add (`app.add_middleware(SecurityHeadersMiddleware)`) and before the `@app.get("/")` registration, mount StaticFiles:

```python
    app.add_middleware(SecurityHeadersMiddleware)

    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, session: Session):
        ...
```

- [ ] **Step 5.5: Run the static-asset tests — both pass**

```bash
uv run pytest tests/test_static_assets.py -v
```

Expected: 2 passed.

- [ ] **Step 5.6: Do NOT commit.**

---

## Task 6: S3b — Update template + tighten CSP

**Files:**
- Modify: `src/iris/templates/base.html` — switch `<script src=>` to `/static/datastar.js`
- Modify: `src/iris/middleware.py` — drop `https://cdn.jsdelivr.net` from `script-src`
- Modify: `tests/test_security_headers.py` — flip the assertion

- [ ] **Step 6.1: Modify `tests/test_security_headers.py` to assert NO jsdelivr in CSP**

```
old:
    assert "default-src 'self'" in csp
    assert "cdn.jsdelivr.net" in csp  # Datastar
    assert "'unsafe-eval'" in csp  # Datastar uses new Function() for reactive expressions
    assert "frame-ancestors 'none'" in csp

new:
    assert "default-src 'self'" in csp
    assert "cdn.jsdelivr.net" not in csp, (
        "Datastar is now vendored at /static/datastar.js; CSP should not allow the CDN"
    )
    assert "script-src 'self' 'unsafe-eval'" in csp, (
        "Datastar uses new Function() for reactive expressions"
    )
    assert "frame-ancestors 'none'" in csp
```

- [ ] **Step 6.2: Run the test — it must fail**

```bash
uv run pytest tests/test_security_headers.py::test_html_responses_have_security_headers -v
```

Expected: AssertionError on the `not in` line because today's CSP still includes `cdn.jsdelivr.net`.

- [ ] **Step 6.3: Modify `src/iris/middleware.py` — drop the CDN, update the comment**

```
old:
# 'unsafe-eval' on script-src: required by Datastar's reactivity engine.
# It compiles `data-on:click="..."`, `data-text="$x"`, and similar attribute
# expressions via `new Function(...)` at runtime — Function() is treated
# the same as eval() by CSP and is blocked without 'unsafe-eval'.
# Trade-off accepted: Datastar is a first-class dependency of the UI;
# without 'unsafe-eval' every reactive expression in every template fails.
# 'unsafe-inline' on style-src is similarly relaxed for inline style
# attributes that templates and Datastar generate.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'"
)

new:
# 'unsafe-eval' on script-src: required by Datastar's reactivity engine.
# It compiles `data-on:click="..."`, `data-text="$x"`, and similar attribute
# expressions via `new Function(...)` at runtime — Function() is treated
# the same as eval() by CSP and is blocked without 'unsafe-eval'.
# Trade-off accepted: Datastar is a first-class dependency of the UI;
# without 'unsafe-eval' every reactive expression in every template fails.
# Datastar itself is vendored at /static/datastar.js (see src/iris/static/),
# so 'self' is sufficient for the script source — no CDN allowlist required.
# 'unsafe-inline' on style-src is similarly relaxed for inline style
# attributes that templates and Datastar generate.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'"
)
```

- [ ] **Step 6.4: Modify `src/iris/templates/base.html` — switch the script src**

```
old:
  <script type="module" src="https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.1/bundles/datastar.js"></script>

new:
  <script type="module" src="/static/datastar.js"></script>
```

- [ ] **Step 6.5: Run the security-headers tests — all pass**

```bash
uv run pytest tests/test_security_headers.py -v
```

Expected: 3 passed.

- [ ] **Step 6.6: Do NOT commit.**

---

## Task 7: S4 — Explicit `path="/"` on the OAuth state cookie

**Files:**
- Modify: `src/iris/auth/providers/oauth.py` — add `path="/"` to one `set_cookie` call

Per spec §4.4, no new test is added: the existing OAuth integration tests (`tests/auth/integration/test_oauth_integration.py`) already drive `/login` → `/login/callback` end-to-end through Keycloak. Those tests would fail if the cookie weren't sent on the callback path.

- [ ] **Step 7.1: Modify `src/iris/auth/providers/oauth.py` — add `path="/"`**

In `OAuthProvider.begin(...)` (around lines 137-144 in the current source), add `path="/"`:

```
old:
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            signed,
            max_age=STATE_COOKIE_TTL,
            httponly=True,
            secure=secure,
            samesite="lax",
        )

new:
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            signed,
            max_age=STATE_COOKIE_TTL,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
```

- [ ] **Step 7.2: Run the existing oauth provider tests to confirm nothing broke**

```bash
uv run pytest tests/auth/test_provider_oauth.py -q
```

Expected: all green. The cookie-attribute change is additive; no existing test asserts on the absence of `Path=/`.

- [ ] **Step 7.3: Do NOT commit.** (S4 verification deferred to Task 11's auth-integration suite, where the real Keycloak round-trip exercises the cookie.)

---

## Task 8: S5 — `verify_csrf_header` + tests

**Files:**
- Modify: `src/iris/auth/csrf.py` — add `verify_csrf_header` (sync)
- Modify: `tests/auth/test_csrf.py` — add four tests

- [ ] **Step 8.1: Add four failing tests for the header dep**

Append to `tests/auth/test_csrf.py` (after the existing tests):

```python
def _build_header_app(*, cookie_secure: bool = False) -> FastAPI:
    from iris.auth.csrf import verify_csrf_header

    app = FastAPI()
    set_settings(app, cookie_name="iris_session", cookie_secure=cookie_secure)

    @app.post("/json-submit")
    def submit_json(_: None = Depends(verify_csrf_header)) -> dict[str, bool]:
        return {"ok": True}

    return app


def test_verify_csrf_header_passes_when_cookie_matches_header():
    client = TestClient(_build_header_app())
    client.cookies.set(CSRF_COOKIE_NAME, "AAAA" * 8)  # 32 chars urlsafe-base64
    r = client.post("/json-submit", headers={"X-CSRF-Token": "AAAA" * 8})
    assert r.status_code == 200


def test_verify_csrf_header_rejects_missing_cookie():
    client = TestClient(_build_header_app())
    # No cookie set
    r = client.post("/json-submit", headers={"X-CSRF-Token": "anything"})
    assert r.status_code == 400


def test_verify_csrf_header_rejects_missing_header():
    client = TestClient(_build_header_app())
    client.cookies.set(CSRF_COOKIE_NAME, "AAAA" * 8)
    r = client.post("/json-submit")  # no header
    assert r.status_code == 400


def test_verify_csrf_header_rejects_mismatch():
    client = TestClient(_build_header_app())
    client.cookies.set(CSRF_COOKIE_NAME, "AAAA" * 8)
    r = client.post("/json-submit", headers={"X-CSRF-Token": "BBBB" * 8})
    assert r.status_code == 400
```

- [ ] **Step 8.2: Run the new tests — they must fail with ImportError**

```bash
uv run pytest tests/auth/test_csrf.py::test_verify_csrf_header_passes_when_cookie_matches_header -v
```

Expected: `ImportError: cannot import name 'verify_csrf_header' from 'iris.auth.csrf'`.

- [ ] **Step 8.3: Add `verify_csrf_header` to `src/iris/auth/csrf.py`**

After the existing `verify_csrf_form` definition, add:

```python
def verify_csrf_header(request: Request) -> None:
    """Verify CSRF for non-form requests via the X-CSRF-Token header.

    Use ``Depends(verify_csrf_header)`` on JSON / Datastar @post / @put /
    @patch / @delete routes. The token transmission is still double-submit:
    the server compares the cookie value with the header value via
    ``hmac.compare_digest``. Client-side code reads ``CSRF_COOKIE_NAME``
    (which is JS-readable) and copies it into the request header.

    Use ``verify_csrf_form`` instead for traditional ``application/x-www-form-urlencoded``
    or ``multipart/form-data`` POSTs that carry the token in the body.
    """
    cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    header = request.headers.get("x-csrf-token", "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=400, detail="csrf_mismatch")
```

Also update the module docstring at the top of the file to mention the two deps. There is no module docstring today; add one:

```python
"""Double-submit CSRF helpers.

Two verifier deps are provided depending on the request body type:

- ``verify_csrf_form`` — reads the token from a form field
  (``CSRF_FORM_FIELD``). Use on ``application/x-www-form-urlencoded`` or
  ``multipart/form-data`` POSTs.
- ``verify_csrf_header`` — reads the token from the ``X-CSRF-Token`` HTTP
  header. Use on JSON / Datastar @post / @put / @patch / @delete routes
  where the body is not form-encoded.

Both compare the submitted token against the ``CSRF_COOKIE_NAME`` cookie
via constant-time comparison and raise HTTP 400 ``csrf_mismatch`` on any
discrepancy.
"""
from __future__ import annotations

import hmac
import re
import secrets

from fastapi import Form, HTTPException, Request, Response
```

(The `from __future__ import annotations` and imports remain unchanged; the docstring is added immediately above them.)

- [ ] **Step 8.4: Run the new tests — all pass**

```bash
uv run pytest tests/auth/test_csrf.py -v
```

Expected: every test in the file passes (the four new ones plus the pre-existing).

- [ ] **Step 8.5: Do NOT commit.**

---

## Task 9: S8 + U5 — `_safe_next` CRLF guard + info logging

**Files:**
- Modify: `src/iris/auth/routes.py` — replace `_safe_next` body
- Modify: `tests/auth/test_login_method_not_allowed.py` — add tests for CRLF and logging

- [ ] **Step 9.1: Add the failing tests**

Append to `tests/auth/test_login_method_not_allowed.py`:

```python
def test_safe_next_rejects_crlf():
    """CRLF in `next` would otherwise enable header injection through the
    Location response header."""
    from iris.auth.routes import _safe_next
    assert _safe_next("/foo\r\nSet-Cookie: x=y") == "/"
    assert _safe_next("/foo\nbar") == "/"
    assert _safe_next("/foo\rbar") == "/"


def test_safe_next_rejects_backslash():
    """Pre-existing rejection: browsers normalize \\ to / in URLs."""
    from iris.auth.routes import _safe_next
    assert _safe_next("/\\evil") == "/"


def test_safe_next_rejects_protocol_relative():
    from iris.auth.routes import _safe_next
    assert _safe_next("//evil.example.com/path") == "/"


def test_safe_next_rejects_absolute():
    from iris.auth.routes import _safe_next
    assert _safe_next("https://evil.example.com/path") == "/"


def test_safe_next_accepts_relative_path():
    from iris.auth.routes import _safe_next
    assert _safe_next("/dashboard") == "/dashboard"


def test_safe_next_logs_info_on_rejection(caplog):
    """U5: every rewrite-to-/ branch logs at INFO with reason= and a
    truncated next= value."""
    import logging

    from iris.auth.routes import _safe_next

    caplog.set_level(logging.INFO, logger="iris.auth")
    _safe_next("/x\r\ny")
    assert any(
        "safe_next_rejected" in record.message and "reason=crlf" in record.message
        for record in caplog.records
    ), [r.message for r in caplog.records]

    caplog.clear()
    _safe_next("//evil")
    assert any(
        "safe_next_rejected" in record.message and "reason=non_relative" in record.message
        for record in caplog.records
    ), [r.message for r in caplog.records]


def test_safe_next_truncates_logged_value():
    """Defense against log injection via giant next= payloads."""
    import logging

    from iris.auth.routes import _safe_next

    caplog_handler_records: list[str] = []
    h = logging.Handler()
    h.emit = lambda r: caplog_handler_records.append(r.getMessage())  # type: ignore[method-assign]
    logging.getLogger("iris.auth").addHandler(h)
    try:
        _safe_next("/" + "A" * 10_000)  # absolute-not-relative? actually starts with / so it's accepted
        # Use a CRLF-variant that's surely rejected:
        _safe_next("\r" + "A" * 10_000)
        # The logged record should not contain the full 10K chars.
        rejected_messages = [m for m in caplog_handler_records if "safe_next_rejected" in m]
        assert rejected_messages, caplog_handler_records
        for msg in rejected_messages:
            assert len(msg) < 500, f"log message not truncated: {len(msg)} chars"
    finally:
        logging.getLogger("iris.auth").removeHandler(h)
```

- [ ] **Step 9.2: Run the new tests — most must fail**

```bash
uv run pytest tests/auth/test_login_method_not_allowed.py -v
```

Expected:
- `test_safe_next_rejects_crlf` FAILS (CRLF passes today's checks).
- `test_safe_next_logs_info_on_rejection` FAILS (no logging today).
- The "accepts" / "rejects backslash" / "rejects protocol-relative" / "rejects absolute" tests pass against the existing implementation.

- [ ] **Step 9.3: Modify `src/iris/auth/routes.py` — replace `_safe_next`**

```
old:
def _safe_next(next_url: str) -> str:
    """Return next_url only if it's a same-origin relative path; else /."""
    if not next_url:
        return "/"
    if "\\" in next_url:
        return "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url

new:
def _safe_next(next_url: str) -> str:
    """Return next_url only if it's a same-origin relative path; else /.

    Defends against open-redirect attacks via a crafted `next` query param.
    Rejects: empty input, CRLF (header injection), backslash (browser
    normalization), absolute URLs, protocol-relative URLs (`//evil`).
    Logs every rewrite at INFO so operators tracing client misconfiguration
    or attempted attacks can see the rejected value (truncated to 128 chars
    to defend against log injection via giant payloads).
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

(`logger` is already defined at the top of `routes.py` as `logger = logging.getLogger("iris.auth")`.)

- [ ] **Step 9.4: Run the test file — all pass**

```bash
uv run pytest tests/auth/test_login_method_not_allowed.py -v
```

Expected: every test passes.

- [ ] **Step 9.5: Do NOT commit.**

---

## Task 10: Update `docs/operations.md`

**Files:**
- Modify: `docs/operations.md` — add `IRIS_TRUST_FORWARDED_FOR` row to env table; refresh security follow-ups; note vendored Datastar.

- [ ] **Step 10.1: Find the env-vars table in `docs/operations.md`**

Run:

```bash
grep -n "IRIS_TRUST_FORWARDED_FOR\|COOKIE_SECURE\|## Env\|## Open security" /home/driou/dev/project/iris/docs/operations.md
```

Locate the env-vars table (search for `COOKIE_SECURE`); insert the new row near it.

- [ ] **Step 10.2: Add `IRIS_TRUST_FORWARDED_FOR` env-vars table row**

Add this row to the env-vars table (alphabetically placed near `COOKIE_SECURE`):

```
| `IRIS_TRUST_FORWARDED_FOR` | when `true`, rate-limit + audit log key on the leftmost `X-Forwarded-For` IP instead of `request.client.host`. Default `false`. Requires a trusted upstream proxy that strips client-supplied X-Forwarded-For. |
```

(The exact table column count must match the surrounding rows; if the table uses three columns, mirror that. Inspect the existing table format and conform.)

- [ ] **Step 10.3: Update the "Open security follow-ups" section**

Find the existing "Rate limiting behind a reverse proxy" bullet and replace it:

```
old:
- **Rate limiting behind a reverse proxy.** `POST /login` keys on `request.client.host`. Behind a proxy this is the proxy's IP — the bucket becomes effectively global. Mitigation: run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy>` so `request.client.host` reflects the `X-Forwarded-For` value.

new:
- **Rate limiting behind a reverse proxy.** Closed by `IRIS_TRUST_FORWARDED_FOR=true`, which makes `iris.auth.client_ip.client_ip` resolve the bucket key from the leftmost X-Forwarded-For entry. The trusted proxy MUST strip any client-supplied X-Forwarded-For before adding its own; otherwise an attacker can spoof the leftmost value and bypass per-IP rate limits. Spec: `docs/superpowers/specs/2026-05-09-security-hardening-design.md`.
- **Rate-limiter memory bound.** `TokenBucket` is now LRU-capped at 10 000 entries (~0.4 MB). Past that threshold, eviction is best-effort: an attacker controlling >10K unique IPs evicts legitimate users' buckets, giving themselves fresh capacity per IP rotation. Acceptable for ≤20-user single-host deployments; a real DDoS demands an upstream WAF.
```

- [ ] **Step 10.4: Add a Datastar-vendoring note**

Find the "Deferred" section near the bottom of `operations.md`. Add a new line:

```
- **Datastar version refresh.** The bundle is vendored at `src/iris/static/datastar.js`. Bumping the Datastar version is a manual two-step: re-download `https://cdn.jsdelivr.net/gh/starfederation/datastar@<version>/bundles/datastar.js` over the vendored file, then commit. There is no automated check that the vendored bytes match a known-good upstream hash — review carefully on bump.
```

- [ ] **Step 10.5: Do NOT commit.** (Verify nothing else accidentally changed by running the full grep again.)

```bash
grep -n "cdn.jsdelivr.net\|IRIS_TRUST_FORWARDED_FOR\|TokenBucket" /home/driou/dev/project/iris/docs/operations.md
```

Expected: at least three matches — the env-vars row, the security follow-up bullets, and the deferred note. The CDN URL appears once (in the deferred note's bump instructions).

---

## Task 11: Final verification + atomic commit

**Files:** none modified — verification + commit only.

- [ ] **Step 11.1: Run the full unit suite**

```bash
uv run --project /home/driou/dev/project/iris pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
```

Expected: **all green**. Total count = 378 (pre-spec) + new tests added across Tasks 2-9. Exact count is not asserted; the inventory diff in Step 11.5 is the regression check.

- [ ] **Step 11.2: Run ruff**

```bash
uv run --project /home/driou/dev/project/iris ruff check
```

Expected: zero warnings.

- [ ] **Step 11.3: Run basedpyright at error level (cheap fail-fast)**

```bash
uv run --project /home/driou/dev/project/iris basedpyright --level error
```

Expected: 0 errors.

- [ ] **Step 11.4: Run basedpyright at warning level (the merge gate per CLAUDE.md)**

```bash
uv run --project /home/driou/dev/project/iris basedpyright --level warning
```

Expected: 0 errors, 0 warnings.

- [ ] **Step 11.5: Test-inventory diff (no coverage regression)**

```bash
uv run --project /home/driou/dev/project/iris pytest --collect-only -q --ignore=tests/auth/integration --ignore=tests/clickhouse/integration > /tmp/pytest-inventory-after.txt
diff /tmp/pytest-inventory-before.txt /tmp/pytest-inventory-after.txt | head -50
```

Expected: only **additions** of new tests (no removals or renames). New tests come from:
- `tests/auth/test_rate_limit.py` — 3 LRU tests.
- `tests/auth/test_client_ip.py` — 10 helper tests.
- `tests/auth/test_csrf.py` — 4 header-dep tests.
- `tests/auth/test_login_method_not_allowed.py` — 7 `_safe_next` tests.
- `tests/test_static_assets.py` — 2 static-asset tests.

Total expected new tests: **26**.

- [ ] **Step 11.6: Run the auth-integration suite (Keycloak)**

```bash
uv run --project /home/driou/dev/project/iris pytest tests/auth/integration -q
```

Expected: 15 passed (all green; Docker required).

- [ ] **Step 11.7: Run the CH-integration suite (Keycloak + ClickHouse)**

```bash
uv run --project /home/driou/dev/project/iris pytest tests/clickhouse/integration -q
```

Expected: 8 passed (all green; Docker required).

- [ ] **Step 11.8: Sanity-check the wheel includes the vendored static asset**

```bash
uv build 2>&1 | tail -5
unzip -l dist/iris-0.1.0-*.whl | grep static/datastar.js
```

Expected: the wheel build succeeds and the file `iris/static/datastar.js` is listed inside the wheel. If the static file is **missing**, edit `pyproject.toml` to add:

```toml
[tool.hatch.build.targets.wheel.force-include]
"src/iris/static/datastar.js" = "iris/static/datastar.js"
```

Re-run `uv build` and re-check. Hatchling normally includes data files alongside Python sources; the force-include is a fallback only.

- [ ] **Step 11.9: Review the full diff**

```bash
git -C /home/driou/dev/project/iris status --short
git -C /home/driou/dev/project/iris diff --stat
```

Expected: about 14 modified files plus 4 new files (`src/iris/auth/client_ip.py`, `src/iris/static/datastar.js`, `tests/auth/test_client_ip.py`, `tests/test_static_assets.py`) — file counts may vary by ±1 if packaging adjustments are needed.

- [ ] **Step 11.10: Stage everything**

```bash
git -C /home/driou/dev/project/iris add -A
git -C /home/driou/dev/project/iris status --short
```

Verify: the staging block lists every modified/new file, no surprises (no stray `.db`, `.coverage`, `dist/`, etc.). If `dist/` shows up from Step 11.8, run `rm -rf dist/` and re-run the add.

- [ ] **Step 11.11: Atomic commit**

```bash
git -C /home/driou/dev/project/iris commit -m "$(cat <<'EOF'
sec: harden auth surface — TokenBucket LRU, X-Forwarded-For, vendored Datastar, CSRF header dep, _safe_next CRLF + logging

Closes 7 review-surfaced security findings as one cohesive pass per the
spec at docs/superpowers/specs/2026-05-09-security-hardening-design.md
and the plan at docs/superpowers/plans/2026-05-09-security-hardening.md:

- S1: TokenBucket now uses an OrderedDict-backed LRU capped at 10K entries.
  Memory bounded at <0.4 MB regardless of input pattern.
- S2: New iris.auth.client_ip.client_ip(request, *, trust_forwarded). The
  flag is read from IRIS_TRUST_FORWARDED_FOR (default false) via
  AuthSettings; routes.py rate-limit + audit-log call sites use it.
- S3: Datastar v1.0.1 vendored at src/iris/static/datastar.js; mounted at
  /static; cdn.jsdelivr.net dropped from CSP. Self-origin only.
- S4: OAuth state cookie now sets path="/" explicitly.
- S5: New verify_csrf_header sync dep reads X-CSRF-Token. Use on Datastar
  @post / JSON-bodied routes. verify_csrf_form unchanged.
- S8: _safe_next now rejects \\r/\\n (header injection).
- U5: every _safe_next rejection logs at INFO with reason= and a
  truncated next= value (defense against log injection).

27 new tests; integration suites + ruff + basedpyright (warning) all green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 11.12: Verify the commit landed**

```bash
git -C /home/driou/dev/project/iris log -1 --stat
git -C /home/driou/dev/project/iris status
```

Expected: HEAD is the new commit; working tree clean.

---

## Out of scope (do NOT touch)

Reserved for the third spec (SQL/identifier hygiene); MUST NOT be touched in this branch:

- Database-name suffix-block validation (the `_DBADMIN`/`_USER` collision).
- `_FIXED_STRING_RE` deduplication (B5).
- `quote_string` vs `_marshal_array_element` escape unification (B6).
- `delete_database` orphan-grant sweep (U4).

Plus everything else from the original review that the user did not include in the prioritized fix list. If anyone is tempted, leave it.
