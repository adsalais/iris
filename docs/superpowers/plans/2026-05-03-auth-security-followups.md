# Auth Security Follow-ups (v1.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Address the security review findings on the auth feature shipped in v1: close the three high-severity gaps (LDAP injection, LDAP plaintext, OAuth cookie), tighten medium-severity defense-in-depth, and clean up operational rough edges.

**Architecture:** Each fix is local to a single auth-package file (one file per task). The public dep surface (`CurrentUser`, `SessionData`, etc.) is unchanged. Tests are added or extended in the existing `tests/auth/test_*.py` files; no new test buckets.

**Tech Stack:** Existing — FastAPI / Starlette, `ldap3`, `httpx`, `itsdangerous`. New deps where unavoidable: `pyjwt[crypto]` for `id_token` verification (M1), `slowapi` or hand-rolled token bucket for rate limiting (M3 — hand-rolled to keep deps lean).

**Spec / source:** `CLAUDE.md` § "Open security follow-ups (v1.1)" plus the additional findings surfaced in the 2026-05-03 security review.

---

## Pre-flight

- Working tree clean. `uv run pytest` → 79 passed + 1 skipped (current main).
- All commits use explicit `git add <listed-paths>` — never `git add -A`.
- Run the **full suite** after every task; the test count grows monotonically (or stays flat for doc-only tasks).
- After each task is committed, the project should be deployable as-is — no half-wired states across commits.

## Phasing

- **Phase A** (must ship): H1, H2, H3 — close the high-severity gaps.
- **Phase B** (should ship): M1–M7 — defense-in-depth.
- **Phase C** (nice to have): L1–L6 — operational polish.

The phases are independent except where noted. Phase A and B can be merged in any order. Phase C tasks are individually tiny and can be slotted in anywhere.

---

## Phase A — High severity

### Task A1: LDAP DN injection (H1)

**Files:**
- Modify: `src/iris/auth/providers/ldap.py`
- Modify: `tests/auth/test_provider_ldap.py`

The `bind_dn_template.format(username=...)` substitution lets DN metacharacters (`,`, `=`, `+`, `<`, `>`, `;`, `"`, `\`, NUL, leading `#`/space) flow into the bind DN and the `(member=<dn>)` filter. Mitigations:

1. Validate `username` against a strict charset before formatting.
2. Escape the resulting DN before substituting into the LDAP filter.

The plan picks a conservative charset suitable for typical corporate directories (alphanumeric + `._-`, max 64 chars). Operators with unusual usernames (dots, accented characters) can widen the regex via a config knob in a future task; for v1.1 strict.

- [ ] **Step 1: Write the failing tests**

In `tests/auth/test_provider_ldap.py`, add (at the bottom):

```python
def test_authenticate_rejects_dn_injection_in_username(provider):
    """Usernames containing DN metacharacters are rejected before bind."""
    import asyncio

    payloads = [
        "alice,ou=evil,dc=corp,dc=local",
        "alice=admin",
        "alice;ou=evil",
        "alice\\ou=evil",
        'alice"ou=evil',
        "alice\x00",
        "alice<>",
        "",                  # empty
        "a" * 65,            # over length cap
        " alice",            # leading whitespace
    ]
    for p in payloads:
        with pytest.raises(AuthError) as exc:
            asyncio.run(provider.authenticate(p, "anything"))
        assert exc.value.token == "invalid_credentials", f"username={p!r} should be rejected"


def test_authenticate_accepts_normal_usernames(provider):
    """Allowed: letters, digits, underscore, dot, hyphen, up to 64 chars."""
    import asyncio

    user = asyncio.run(provider.authenticate("alice", "secret"))
    assert user.subject == "uid=alice,ou=people,dc=corp,dc=local"
```

- [ ] **Step 2: Run — confirm failures**

```bash
uv run pytest tests/auth/test_provider_ldap.py -v
```

Expected: at least one of the new payload-cases fails (the existing impl would attempt to bind with the injected DN and likely fail with `invalid_credentials` only by chance).

- [ ] **Step 3: Implement the charset whitelist + filter escape**

In `src/iris/auth/providers/ldap.py`, add at the top:

```python
import re

from ldap3.utils.conv import escape_filter_chars

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
```

Update `authenticate`:

```python
    async def authenticate(self, username: str, password: str) -> User:
        if not _USERNAME_RE.fullmatch(username):
            raise AuthError("invalid_credentials")
        bind_dn = self._settings.bind_dn_template.format(username=username)
        try:
            conn = self._open_connection(bind_dn, password)
        except _BindFailed:
            raise AuthError("invalid_credentials")
        except _Unreachable:
            logger.exception("auth: LDAP unreachable")
            raise AuthError("ldap_unreachable")

        try:
            display_name = self._read_display_name(conn, bind_dn) or username
            groups = self._read_groups(conn, bind_dn)
        except Exception:
            logger.exception("auth: LDAP group/profile read failed")
            raise AuthError("ldap_groups")

        return User(subject=bind_dn, display_name=display_name, groups=tuple(groups))
```

Update `_read_groups` to escape the DN inside the filter:

```python
    def _read_groups(self, conn: Connection, bind_dn: str) -> list[str]:
        conn.search(
            self._settings.group_base_dn,
            f"(member={escape_filter_chars(bind_dn)})",
            attributes=["cn"],
        )
        groups: list[str] = []
        for entry in conn.entries:
            cn = entry.cn.value if "cn" in entry else None
            if cn:
                groups.append(str(cn))
        return groups
```

(Also narrows `attributes=ALL_ATTRIBUTES` to `["cn"]`, addressing reviewer's earlier minor.)

- [ ] **Step 4: Run — confirm green**

```bash
uv run pytest tests/auth/ -v
```

Expected: 4 prior LDAP tests + 2 new = 6 passed in the bucket; full suite `79 + 2 = 81` passed.

- [ ] **Step 5: Commit**

```bash
git add src/iris/auth/providers/ldap.py tests/auth/test_provider_ldap.py
git commit -m "Reject LDAP DN injection via username charset whitelist"
```

---

### Task A2: LDAP TLS configuration (H2)

**Files:**
- Modify: `src/iris/auth/config.py`
- Modify: `src/iris/auth/providers/ldap.py`
- Modify: `tests/auth/test_config.py`
- Modify: `tests/auth/test_provider_ldap.py` (only if the connection-factory contract changes; otherwise no)
- Modify: `.env` (operator-facing)
- Modify: `CLAUDE.md`

Add a `LDAP_REQUIRE_TLS` boolean (default `True`) and an optional `LDAP_CA_CERT_PATH`. When `LDAP_REQUIRE_TLS=true`, plaintext `ldap://` URLs are rejected at config-load time. When set to a CA path, the `ldap3.Server(...)` constructor receives a `Tls(ca_certs_file=...)` instance.

- [ ] **Step 1: Failing tests in `tests/auth/test_config.py`**

```python
def test_ldap_url_plaintext_rejected_when_tls_required(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "ldap")
    monkeypatch.setenv("LDAP_URL", "ldap://ldap.example.com:389")  # plaintext
    monkeypatch.setenv("LDAP_BIND_DN_TEMPLATE", "uid={username},ou=people,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_GROUP_BASE_DN", "ou=groups,dc=corp,dc=local")
    # LDAP_REQUIRE_TLS defaults to True
    with pytest.raises(ValueError, match="LDAP_URL"):
        AuthSettings.from_env()


def test_ldap_url_plaintext_allowed_when_tls_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("AUTH_METHOD", "ldap")
    monkeypatch.setenv("LDAP_URL", "ldap://ldap.example.com:389")
    monkeypatch.setenv("LDAP_BIND_DN_TEMPLATE", "uid={username},ou=people,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_GROUP_BASE_DN", "ou=groups,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_REQUIRE_TLS", "false")
    s = AuthSettings.from_env()
    assert s.ldap.url == "ldap://ldap.example.com:389"
    assert s.ldap.require_tls is False


def test_ldap_ca_cert_path_loaded(monkeypatch, tmp_path):
    fake_ca = tmp_path / "ca.pem"
    fake_ca.write_text("-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n")
    monkeypatch.setenv("AUTH_METHOD", "ldap")
    monkeypatch.setenv("LDAP_URL", "ldaps://ldap.example.com:636")
    monkeypatch.setenv("LDAP_BIND_DN_TEMPLATE", "uid={username},ou=people,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_GROUP_BASE_DN", "ou=groups,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_CA_CERT_PATH", str(fake_ca))
    s = AuthSettings.from_env()
    assert s.ldap.ca_cert_path == str(fake_ca)
```

- [ ] **Step 2: Run — confirm failures** (`AttributeError: 'LDAPSettings' has no attribute 'require_tls'` etc.)

- [ ] **Step 3: Implement `LDAPSettings` extension**

In `src/iris/auth/config.py`, replace `LDAPSettings`:

```python
@dataclass(frozen=True)
class LDAPSettings:
    url: str
    bind_dn_template: str
    group_base_dn: str
    require_tls: bool
    ca_cert_path: str | None
```

Update the `AuthSettings.from_env` LDAP branch:

```python
        elif method == "ldap":
            url = _get_required("LDAP_URL")
            require_tls = _get_bool("LDAP_REQUIRE_TLS", True)
            if require_tls and not url.startswith("ldaps://"):
                raise ValueError(
                    f"LDAP_URL must use ldaps:// when LDAP_REQUIRE_TLS=true; got {url!r}. "
                    "Set LDAP_REQUIRE_TLS=false to allow plaintext (development only)."
                )
            ldap = LDAPSettings(
                url=url,
                bind_dn_template=_get_required("LDAP_BIND_DN_TEMPLATE"),
                group_base_dn=_get_required("LDAP_GROUP_BASE_DN"),
                require_tls=require_tls,
                ca_cert_path=os.environ.get("LDAP_CA_CERT_PATH") or None,
            )
```

- [ ] **Step 4: Wire CA cert into `LDAPProvider._open_connection`**

In `src/iris/auth/providers/ldap.py`, update the production `_open_connection` branch:

```python
        try:
            tls = None
            if self._settings.ca_cert_path:
                from ldap3 import Tls
                import ssl
                tls = Tls(
                    validate=ssl.CERT_REQUIRED,
                    ca_certs_file=self._settings.ca_cert_path,
                )
            server = Server(self._settings.url, get_info=None, tls=tls)
            conn = Connection(server, user=bind_dn, password=password, auto_bind=True)
            return conn
        except Exception as exc:
            ...
```

- [ ] **Step 5: Run the full suite**

```bash
uv run pytest -v
```

Expected: previous tests still pass + 3 new config tests = 84 passed.

- [ ] **Step 6: Update `.env` with the new vars**

Append to the LDAP section in `.env`:

```
# Reject plaintext ldap:// URLs at startup. Default: true. Set to false ONLY
# for trusted-network dev setups.
LDAP_REQUIRE_TLS=true

# Optional path to a CA bundle for verifying the LDAP server's TLS cert.
# If unset, ldap3 falls back to the system trust store.
# LDAP_CA_CERT_PATH=/etc/ssl/certs/ca-bundle.crt
```

- [ ] **Step 7: Document in `CLAUDE.md`**

In the configuration block (the env-var listing under "Authentication"), add `LDAP_REQUIRE_TLS=true` and `LDAP_CA_CERT_PATH=...` lines with short descriptions.

Remove the corresponding entry from the "Open security follow-ups (v1.1)" section.

- [ ] **Step 8: Commit**

```bash
git add src/iris/auth/config.py src/iris/auth/providers/ldap.py tests/auth/test_config.py .env CLAUDE.md
git commit -m "Add LDAP_REQUIRE_TLS and LDAP_CA_CERT_PATH; reject plaintext ldap:// by default"
```

---

### Task A3: OAuth state cookie respects `cookie_secure` (H3)

**Files:**
- Modify: `src/iris/auth/providers/oauth.py`
- Modify: `tests/auth/test_provider_oauth.py`

The OAuth state cookie is currently `secure=False` hardcoded. Read `cookie_secure` from `request.app.state.auth_cookie_secure` (same pattern as CSRF — Task 6 follow-up).

- [ ] **Step 1: Failing test**

In `tests/auth/test_provider_oauth.py`, add:

```python
def test_oauth_state_cookie_follows_cookie_secure(provider):
    """The oauth_state cookie's Secure flag should follow app.state.auth_cookie_secure."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    for cookie_secure in (True, False):
        app = FastAPI()
        app.state.auth_cookie_secure = cookie_secure

        @app.get("/login", name="login_callback")  # name needed for url_for
        async def login(request):
            return await provider.begin(request)

        r = TestClient(app).get("/login", follow_redirects=False)
        assert r.status_code == 302
        set_cookie = r.headers["set-cookie"].lower()
        assert ("secure" in set_cookie) == cookie_secure
```

- [ ] **Step 2: Run — confirm failure**

The current code hard-codes `secure=False`, so the `cookie_secure=True` branch fails.

- [ ] **Step 3: Implement**

In `src/iris/auth/providers/oauth.py`, in `begin()`:

```python
        secure = getattr(request.app.state, "auth_cookie_secure", True)
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            signed,
            max_age=STATE_COOKIE_TTL,
            httponly=True,
            secure=secure,
            samesite="lax",
        )
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/auth/test_provider_oauth.py -v
git add src/iris/auth/providers/oauth.py tests/auth/test_provider_oauth.py
git commit -m "Wire cookie_secure through OAuth state cookie"
```

---

## Phase B — Medium severity

### Task B1: `id_token` JWT verification (M1)

**Files:**
- Modify: `pyproject.toml` (add `pyjwt[crypto]`)
- Modify: `src/iris/auth/providers/oauth.py`
- Modify: `tests/auth/test_provider_oauth.py`

Fetch the IdP's JWKS at discovery time, then verify each `id_token` returned from the token endpoint: signature, issuer, audience (must match `client_id`), expiry, not-before. Use `pyjwt[crypto]`.

The userinfo path stays — userinfo is still the source of truth for `groups` etc. (some IdPs put groups only in userinfo, not the id_token). But the id_token signature gives us a cryptographic guarantee that the IdP did issue these claims.

- [ ] **Step 1: Add dep**

```bash
uv add 'pyjwt[crypto]'
```

- [ ] **Step 2: Failing test**

In `tests/auth/test_provider_oauth.py`, extend the `_mock_transport()` to serve a JWKS document and a properly-signed `id_token`. Add:

```python
def test_exchange_code_rejects_unsigned_id_token(provider_factory_with_bad_id_token):
    """If id_token is missing or has a bad signature, exchange fails."""
    import asyncio
    import pytest
    from iris.auth.exceptions import AuthError

    provider = provider_factory_with_bad_id_token()
    with pytest.raises(AuthError) as exc:
        asyncio.run(
            provider.exchange_code(
                code="dummy",
                code_verifier="v",
                redirect_uri="http://localhost/login/callback",
            )
        )
    assert exc.value.token == "oauth_exchange"


def test_exchange_code_accepts_valid_id_token(provider):
    """A properly signed id_token from the IdP's JWKS is accepted."""
    import asyncio
    user = asyncio.run(
        provider.exchange_code(
            code="dummy",
            code_verifier="v",
            redirect_uri="http://localhost/login/callback",
        )
    )
    assert user.subject == "abc-123"
```

The test fixture work is non-trivial: generate an RSA keypair in `_mock_transport()`, expose the public part via `/.well-known/jwks.json`, sign a fake id_token with the private part, and add `jwks_uri` to the discovery doc.

- [ ] **Step 3: Implement**

Discovery time — also fetch JWKS:

```python
        self.userinfo_endpoint: str = doc["userinfo_endpoint"]
        self.jwks_uri: str = doc["jwks_uri"]
        self._jwks_client = jwt.PyJWKClient(self.jwks_uri)
```

In `exchange_code`, after parsing the token response, before the userinfo call:

```python
            id_token = r.json().get("id_token")
            if not id_token:
                raise AuthError("oauth_exchange")
            try:
                signing_key = self._jwks_client.get_signing_key_from_jwt(id_token).key
                jwt.decode(
                    id_token,
                    signing_key,
                    algorithms=["RS256", "ES256"],
                    audience=self._settings.client_id,
                    issuer=self._settings.issuer_url.rstrip("/"),
                )
            except jwt.InvalidTokenError as exc:
                logger.exception("auth: id_token verification failed")
                raise AuthError("oauth_exchange") from exc
```

- [ ] **Step 4: Run + commit**

```bash
uv run pytest tests/auth/test_provider_oauth.py -v
git add pyproject.toml uv.lock src/iris/auth/providers/oauth.py tests/auth/test_provider_oauth.py
git commit -m "Verify id_token signature, issuer, and audience via PyJWT"
```

---

### Task B2: LDAP typed exception classification (M2)

**Files:**
- Modify: `src/iris/auth/providers/ldap.py`
- Modify: `tests/auth/test_provider_ldap.py`

Replace `if "invalidcredentials" in msg or "invalid credentials" in msg:` with typed catches.

- [ ] **Step 1: Failing test**

```python
def test_open_connection_classifies_invalid_credentials_via_typed_exception(settings):
    """Auth failures are surfaced as _BindFailed regardless of locale."""
    from ldap3.core.exceptions import LDAPInvalidCredentialsResult
    from iris.auth.providers.ldap import LDAPProvider, _BindFailed

    def factory():
        # A connection whose rebind() raises the typed exception
        class _C:
            def rebind(self, *, user, password):
                raise LDAPInvalidCredentialsResult(result=49)
        return _C()

    provider = LDAPProvider(settings, _connection_factory=factory)
    import pytest
    with pytest.raises(_BindFailed):
        provider._open_connection("uid=x,...", "pw")


def test_open_connection_classifies_socket_open_as_unreachable(settings):
    from ldap3.core.exceptions import LDAPSocketOpenError
    from iris.auth.providers.ldap import LDAPProvider, _Unreachable

    def factory():
        class _C:
            def rebind(self, *, user, password):
                raise LDAPSocketOpenError("connection refused")
        return _C()

    provider = LDAPProvider(settings, _connection_factory=factory)
    import pytest
    with pytest.raises(_Unreachable):
        provider._open_connection("uid=x,...", "pw")
```

- [ ] **Step 2: Run — confirm failure** (current substring match doesn't see the typed exception via the `_connection_factory` path because that path uses `rebind()` returning a bool, not raising — so we'd need to extend `_open_connection` to also handle exceptions from the factory).

- [ ] **Step 3: Implement**

```python
from ldap3.core.exceptions import (
    LDAPBindError,
    LDAPException,
    LDAPInvalidCredentialsResult,
    LDAPSocketOpenError,
)


    def _open_connection(self, bind_dn: str, password: str) -> Connection:
        if self._connection_factory is not None:
            conn = self._connection_factory()
            try:
                ok = conn.rebind(user=bind_dn, password=password)
            except LDAPInvalidCredentialsResult:
                raise _BindFailed()
            except (LDAPSocketOpenError, LDAPException):
                raise _Unreachable()
            if not ok:
                raise _BindFailed()
            return conn
        try:
            ...  # production path
            return conn
        except LDAPInvalidCredentialsResult as exc:
            raise _BindFailed() from exc
        except (LDAPSocketOpenError, LDAPException) as exc:
            raise _Unreachable() from exc
        except Exception as exc:  # last-resort fallback
            raise _Unreachable() from exc
```

- [ ] **Step 4: Run + commit**

```bash
git add src/iris/auth/providers/ldap.py tests/auth/test_provider_ldap.py
git commit -m "Classify LDAP exceptions by type, not by message substring"
```

---

### Task B3: Login rate limiting (M3)

**Files:**
- Create: `src/iris/auth/rate_limit.py`
- Modify: `src/iris/auth/routes.py`
- Create: `tests/auth/test_rate_limit.py`

Hand-rolled token bucket per `(client_ip, "login")`. Default: 10 requests / 60s burst, then 1 / 5s sustained. On exhaustion, return 429 with `Retry-After` header.

- [ ] **Step 1: Failing test**

```python
# tests/auth/test_rate_limit.py
import pytest
from fastapi.testclient import TestClient
from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD


@pytest.fixture
def client():
    from iris.app import build_app
    return TestClient(build_app())


def test_login_rate_limit_kicks_in_on_burst(client):
    """After N rapid POSTs to /login from the same IP, returns 429."""
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    body = {CSRF_FORM_FIELD: csrf, "username": "alice", "password": "wrong", "next": "/"}
    last_status = None
    for _ in range(15):  # past the 10/min burst
        last_status = client.post("/login", data=body, follow_redirects=False).status_code
    assert last_status == 429
```

- [ ] **Step 2: Implement**

```python
# src/iris/auth/rate_limit.py
import time
from collections import defaultdict


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float):
        self.capacity = capacity
        self.refill = refill_per_second
        self._buckets: dict[str, tuple[float, float]] = defaultdict(
            lambda: (capacity, time.monotonic())
        )

    def take(self, key: str) -> float | None:
        """Returns None if allowed, or seconds-to-wait if rate-limited."""
        now = time.monotonic()
        tokens, last = self._buckets[key]
        tokens = min(self.capacity, tokens + (now - last) * self.refill)
        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            return None
        self._buckets[key] = (tokens, now)
        return (1 - tokens) / self.refill
```

In `routes.py`, instantiate one bucket per app and call it from `login_post`:

```python
    bucket = TokenBucket(capacity=10, refill_per_second=0.2)  # 1 every 5s sustained, burst 10

    @router.post("/login")
    async def login_post(request: Request, ...):
        client_ip = request.client.host if request.client else "unknown"
        wait = bucket.take(f"login:{client_ip}")
        if wait is not None:
            return Response(
                status_code=429,
                headers={"Retry-After": str(int(wait) + 1)},
            )
        ...
```

- [ ] **Step 3: Run + commit**

```bash
git add src/iris/auth/rate_limit.py src/iris/auth/routes.py tests/auth/test_rate_limit.py
git commit -m "Add token-bucket rate limit on POST /login"
```

---

### Task B4: Absolute session expiry (M4)

**Files:**
- Modify: `src/iris/auth/identity.py` (UserSession gets `absolute_expires_at`)
- Modify: `src/iris/auth/sessions.py`
- Modify: `src/iris/auth/config.py` (new `SESSION_ABSOLUTE_TTL_SECONDS`)
- Modify: `tests/auth/test_session_store.py`
- Modify: `tests/auth/test_config.py`

Add `SESSION_ABSOLUTE_TTL_SECONDS` (default `2592000` = 30 days). `UserSession` gains an `absolute_expires_at` set at creation. `get_and_refresh` evicts when EITHER `expires_at <= now` OR `absolute_expires_at <= now`.

- [ ] **Step 1: Failing test**

```python
def test_absolute_expiry_overrides_sliding_refresh():
    """Even with constant refresh, the absolute deadline kicks in."""
    import asyncio
    from datetime import datetime, timedelta, UTC
    store = InMemorySessionStore(ttl_seconds=60, absolute_ttl_seconds=120)
    user = User(subject="alice", display_name="Alice", groups=())
    session = asyncio.run(store.create(user))
    # forcibly age the absolute deadline
    object.__setattr__(session, "absolute_expires_at", datetime.now(UTC) - timedelta(seconds=1))
    assert asyncio.run(store.get_and_refresh(session.id)) is None
```

- [ ] **Step 2: Implement** — straightforward:

`identity.py`:
```python
@dataclass(slots=True)
class UserSession:
    ...
    absolute_expires_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
```

`sessions.py`:
```python
class InMemorySessionStore:
    def __init__(self, ttl_seconds: int, absolute_ttl_seconds: int) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._absolute_ttl = timedelta(seconds=absolute_ttl_seconds)
        ...

    async def create(self, user: User) -> UserSession:
        async with self._lock:
            now = datetime.now(UTC)
            session = UserSession(
                ...
                expires_at=now + self._ttl,
                absolute_expires_at=now + self._absolute_ttl,
            )
            ...

    async def get_and_refresh(self, session_id: str) -> UserSession | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            now = datetime.now(UTC)
            if session.expires_at <= now or session.absolute_expires_at <= now:
                del self._sessions[session_id]
                return None
            session.expires_at = now + self._ttl
            return session
```

`config.py` adds `absolute_ttl_seconds: int` to `AuthSettings`. `routes.install` passes it through.

- [ ] **Step 3: Run + commit**

```bash
git add src/iris/auth/identity.py src/iris/auth/sessions.py src/iris/auth/config.py src/iris/auth/routes.py tests/auth/ .env CLAUDE.md
git commit -m "Add SESSION_ABSOLUTE_TTL_SECONDS for hard session lifetime cap"
```

---

### Task B5: CSRF token rotation on login (M5)

**Files:**
- Modify: `src/iris/auth/routes.py`
- Modify: `src/iris/auth/csrf.py` (add `delete_csrf_cookie` helper)
- Modify: `tests/auth/test_csrf.py`

After `store.create(user)` in `login_post` and `login_callback`, clear the CSRF cookie. The next form render will mint a fresh one. This invalidates any pre-login captured token.

- [ ] **Step 1: Failing test**

```python
def test_csrf_token_rotates_after_login(client):
    """The CSRF cookie value present before login is invalidated post-login."""
    r = client.get("/login")
    pre_token = r.cookies[CSRF_COOKIE_NAME]
    client.post("/login", data={CSRF_FORM_FIELD: pre_token, "username": "alice", "password": "secret", "next": "/"})
    # After successful login, the old token must not validate.
    r = client.post("/logout", data={CSRF_FORM_FIELD: pre_token}, follow_redirects=False)
    assert r.status_code == 400  # csrf_mismatch (cookie has rotated)
```

- [ ] **Step 2: Implement**

In `csrf.py`:
```python
def delete_csrf_cookie(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
```

In `routes.py`, after `_set_session_cookie(...)` in both `login_post` and `login_callback`:
```python
        delete_csrf_cookie(response)
```

The `index.html` route has `Depends(issue_csrf_token)`, which mints a fresh cookie on the post-login redirect target. So the user's next request lands on `/`, gets a fresh CSRF, all good.

- [ ] **Step 3: Run + commit**

---

### Task B6: Security headers middleware (M6)

**Files:**
- Create: `src/iris/middleware.py` (new module — security-headers middleware)
- Modify: `src/iris/app.py` (install middleware in `build_app()`)
- Create: `tests/test_security_headers.py`

Add a small middleware that sets `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, and a conservative `Content-Security-Policy` on HTML responses. CSP needs care — Datastar's CDN must be allowed in `script-src`.

- [ ] **Step 1: Test**

```python
def test_html_responses_have_security_headers(authed_client):
    r = authed_client.get("/")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "Referrer-Policy" in r.headers
    csp = r.headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp
    assert "cdn.jsdelivr.net" in csp  # Datastar
```

- [ ] **Step 2: Implement** — Starlette middleware adding the headers post-response.

- [ ] **Step 3: Commit**

---

### Task B7: Failed-login structured logging (M7)

**Files:**
- Modify: `src/iris/auth/routes.py` (log failed attempts before redirecting)
- Modify: `tests/auth/test_login_mock.py` (assert log line via `caplog`)

Currently a failed `/login` raises `AuthError` and we redirect with `?error=...` but log nothing. Add an INFO log on failure with username + remote addr (no password).

- [ ] **Step 1: Test**

```python
def test_failed_login_logs_attempt(client, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="iris.auth")
    r = client.get("/login")
    csrf = r.cookies[CSRF_COOKIE_NAME]
    client.post("/login", data={CSRF_FORM_FIELD: csrf, "username": "alice", "password": "wrong", "next": "/"})
    assert any(
        "auth: login_failed" in rec.message and "alice" in rec.message
        for rec in caplog.records
    )
```

- [ ] **Step 2: Implement** — wrap the `except AuthError` branch with `logger.info(...)`.

- [ ] **Step 3: Commit**

---

## Phase C — Operational

### Task C1: OIDC discovery — lazy + cached (L1)

Move `_client.get(discovery_url)` out of `__init__` into a property cached on first call. Tests inject `_http_transport` as today; production `build_app()` no longer blocks on the network at startup.

Key constraint: `begin()` and `complete()` both need the discovered endpoints — call the cache miss path on first invocation. If discovery fails, raise `AuthError("oauth_discovery")` and let the route redirect to `/login?error=oauth_discovery`.

### Task C2: Per-user session cap (L3)

`InMemorySessionStore.create(user)` evicts the oldest session for the same `user.subject` if the user has ≥ N (default 10) active sessions. Test: create 11 sessions for "alice", verify the oldest is gone.

### Task C3: httpx client cleanup on app shutdown (L4)

`OAuthProvider.close()` method calls `_client.close()` and `await _async_client.aclose()`. Wire it into FastAPI's `app.add_event_handler("shutdown", ...)` from `routes.install`. Test by inspecting that `close()` is callable without error.

### Task C4: Username length cap at HTTP layer (L5)

Add a `min_length=1, max_length=64` Pydantic constraint on the `Form()` declarations in `login_post`. Returns 422 for over-long input. Doesn't replace the LDAP charset whitelist (Task A1) — defense-in-depth at a different layer.

### Task C5: `.env` permissions doc (L6)

One paragraph in CLAUDE.md recommending `chmod 600 .env`. No code change. No test.

---

## Suggested ordering

If executing serially, ordering by dependency and impact:

1. **A1** (LDAP injection) — biggest correctness win, no deps.
2. **A3** (OAuth cookie secure) — trivially small, parallel-safe.
3. **A2** (LDAP TLS) — adds config fields; touches docs and `.env`.
4. **B5** (CSRF rotation) — small, defense-in-depth.
5. **B4** (absolute expiry) — touches store + identity + config; bigger blast radius, do it next.
6. **B7** (failed-login logging) — tiny, safe to slot anywhere.
7. **B2** (LDAP typed exceptions) — improves Task A1's exception handling consistency.
8. **B1** (id_token verification) — adds dep + non-trivial test setup; tackle when calmer.
9. **B3** (rate limiting) — new module, in-memory state shared by app.
10. **B6** (security headers) — middleware; very low risk.
11. **C1–C5** — slot in opportunistically; each is small.

Total estimated commits: **~15–18**. Total estimated test additions: **~25–30**.

## Out of scope (explicit non-goals)

- Replacing `InMemorySessionStore` with Redis (the v1.1 `--workers 1` constraint stands; this plan doesn't lift it).
- Account lockout / brute-force defense beyond the per-IP rate limit.
- Audit log persistence beyond Python's `logging` module.
- Password complexity requirements (mock provider only — production passwords are the IdP's job).
- TOTP / 2FA.
- Backchannel logout / RP-initiated logout (already deferred in the original v1 spec).
- Pluggable rate-limit backends (in-memory is enough for `--workers 1`).
