# Authentication & Authorization for `iris`

**Date:** 2026-05-03
**Status:** Design — approved through brainstorming; pending user review of this document.

## Context

`iris` is a small FastAPI + Jinja2 + Datastar app. It currently has no authentication. We need to add an auth layer that supports both OAuth (OIDC, with Keycloak as the primary target but not the only one) and LDAP, server-side, with role-based authorization sourced from IdP groups.

The deployment is small (≤ 20 users), session state is allowed to be in-memory (reset on restart is acceptable), and only one auth method is active per deployment (selected by config). A test mock provider is required so that the entire auth pipeline runs unmodified in tests.

## Requirements (locked in via brainstorming)

1. **Authentication methods, server-side:** OIDC and LDAP. OIDC is provider-agnostic via standard discovery (Keycloak-tested but not Keycloak-coupled).
2. **Per-deployment toggle:** exactly one method active at a time, selected by an `AUTH_METHOD` env var. Both implementations live in the codebase.
3. **Authorization:** role-based, **groups passed through from the IdP** verbatim. Routes guard themselves with `require_group(...)` using the IdP group name. The same group names must exist in Keycloak and LDAP for a route guard to work across both deployments — this is on the operator, not on the app.
4. **Sessions:** server-side, in-memory, ≤ 20 users, reset-on-restart acceptable. Sliding TTL of 12 h, refreshed on every authenticated request. Lazy eviction (no background sweeper).
5. **Cookie + bearer transport:** the same session id can be sent as a cookie or as `Authorization: Bearer <session-id>`. There is no separate "API token" concept distinct from sessions.
6. **Front-door redirect:** an unauthenticated user hitting an HTML route is redirected to `/login`; an unauthenticated request to an API route returns `401`. The decision uses the `Accept` header.
7. **Logout:** local-only. Clears the cookie and deletes the server-side session. No RP-initiated logout, no back-channel logout.
8. **Test mockup:** a `mock` provider selected by `AUTH_METHOD=mock` runs the same routes/middleware/deps as the real providers; only the credential check differs.

## Out of scope (explicit non-goals)

- Persistent session storage (Redis, DB).
- Long-lived API tokens distinct from browser sessions.
- Service-to-service / machine-account credentials.
- Per-resource (record-level) authorization.
- RP-initiated or back-channel OIDC logout.
- Multiple OAuth providers active simultaneously.
- Datastar-side handling of mid-session expiry beyond plain 401 (potential v1.1 follow-up: a `datastar-patch-elements` toast).

---

## Architecture

### Module map

```
src/iris/auth/
├── __init__.py        # public surface (re-exports CurrentUser, require_group, the auth router)
├── config.py          # AuthSettings: AUTH_METHOD + provider-specific env vars
├── identity.py        # User dataclass; UserSession dataclass
├── sessions.py        # InMemorySessionStore: create/get_and_refresh/delete; cookie + bearer extraction
├── deps.py            # CurrentUser, OptionalCurrentUser, require_group(...)
├── routes.py          # APIRouter with /login, /login/callback, /logout, /api/whoami
└── providers/
    ├── __init__.py    # factory: AUTH_METHOD → provider instance
    ├── base.py        # Provider Protocol
    ├── oauth.py       # OIDC via Authlib
    ├── ldap.py        # ldap3 bind + group search
    └── mock.py        # accepts a configured (username, password); returns configured groups
```

The `auth` package is self-contained. The rest of `iris` interacts with it via `CurrentUser`, `OptionalCurrentUser`, `require_group(...)`, and the auth router (mounted by `app.py`).

### Identity model

```python
@dataclass(frozen=True, slots=True)
class User:
    subject: str              # stable IdP id: OAuth `sub`, LDAP DN, "mock-user"
    display_name: str
    groups: tuple[str, ...]   # verbatim from the IdP

@dataclass(slots=True)
class UserSession:
    id: str                   # 32 bytes from secrets.token_urlsafe; cookie value AND bearer
    user: User
    created_at: datetime
    expires_at: datetime      # sliding TTL, refreshed on every authenticated request
```

### Session store

`dict[str, UserSession]` guarded by an `asyncio.Lock`. API:

- `create(user) -> UserSession`
- `get_and_refresh(sid) -> UserSession | None` — returns `None` if absent or expired; deletes expired entries; otherwise extends `expires_at`.
- `delete(sid) -> None`

No background sweeper for v1; expired entries are evicted lazily on `get_and_refresh`.

### Provider Protocol

The only abstraction in the design.

```python
class Provider(Protocol):
    """Strategy that turns a login attempt into a User."""
    async def begin(self, request: Request) -> Response: ...
    async def complete(self, request: Request) -> User: ...
```

`begin()` either renders the LDAP/mock form (HTML) or 302s to the IdP. `complete()` consumes whatever came back — POSTed form for LDAP/mock, callback query params for OAuth — and returns a `User`, or raises `AuthError("invalid_credentials" | "ldap_unreachable" | …)` with a stable error token (no free-form message in the URL).

A factory in `providers/__init__.py` reads `AUTH_METHOD` at app startup and constructs the singleton provider. The active provider is exposed as a FastAPI dependency.

### Configuration

```
AUTH_METHOD=oauth | ldap | mock
SESSION_COOKIE_NAME=iris_session
SESSION_TTL_SECONDS=43200            # 12h
COOKIE_SECURE=true                   # set false for local dev over http

# OAuth (OIDC discovery)
OIDC_ISSUER_URL=https://keycloak.example.com/realms/iris
OIDC_CLIENT_ID=iris
OIDC_CLIENT_SECRET=...
OIDC_SCOPES=openid profile email groups

# LDAP
LDAP_URL=ldaps://ldap.example.com:636
LDAP_BIND_DN_TEMPLATE=uid={username},ou=people,dc=corp,dc=local
LDAP_GROUP_BASE_DN=ou=groups,dc=corp,dc=local

# Mock (tests)
MOCK_USERNAME=alice
MOCK_PASSWORD=secret
MOCK_GROUPS=admins,users
MOCK_DISPLAY_NAME=Alice (mock)
```

OIDC discovery happens **at app startup**. If the discovery URL is unreachable, the app fails to start with a clear log line. We do not lazy-discover on first request.

---

## Flows

### Front-door

```
GET /                    →  no valid session  →  302 /login?next=/
GET /api/anything        →  no valid session  →  401 (no body)
```

The redirect-vs-401 decision uses `Accept: text/html`. Both paths are unified in a single FastAPI exception handler that consumes a custom `AuthRequired` exception.

### OAuth login (`AUTH_METHOD=oauth`)

```
GET /login?next=/                 (provider.begin)
   └─ build OIDC authorize URL via Authlib (uses discovery)
   └─ generate state + PKCE code_verifier
   └─ stash {state, code_verifier, next} in a short-lived signed cookie
       ("oauth_state", 10min, HttpOnly, SameSite=Lax)
   └─ 302 to IdP authorize endpoint
        ↓
   user authenticates at IdP
        ↓
GET /login/callback?code=...&state=...   (provider.complete)
   └─ read & delete oauth_state cookie
   └─ verify state matches (CSRF for the OAuth flow)
   └─ exchange code → tokens → userinfo (Authlib)
   └─ build User: subject=claims['sub'],
                   display_name=claims.get('name') or claims['preferred_username'],
                   groups=tuple(claims.get('groups', []))
   └─ create UserSession; set session cookie
   └─ 302 to next
```

Storing `{state, code_verifier, next}` in a signed cookie (rather than the server-side session store) is fine because nothing sensitive is in flight — only a short-lived nonce and a redirect target. Authlib provides the signing primitive.

### LDAP login (`AUTH_METHOD=ldap`)

```
GET /login?next=/                 (provider.begin)
   └─ render templates/auth/ldap_form.html:
       • CSRF token (double-submit: cookie + hidden form field)
       • username, password fields
       • hidden "next" field
       • optional error message rendered from ?error=
        ↓
POST /login                        (provider.complete)
   └─ verify CSRF (cookie value == form field value)
   └─ ldap3.Connection(LDAP_URL,
                       user=LDAP_BIND_DN_TEMPLATE.format(username=...),
                       password=...).bind()
   └─ search LDAP_GROUP_BASE_DN with filter (member=<userDN>)
       → list of group CNs
   └─ build User: subject=userDN, display_name=cn or username, groups=group_cns
   └─ create UserSession; set session cookie
   └─ 302 to next
```

We bind first, then search for groups via `(member=<dn>)` rather than reading the user's `memberOf` attribute. `memberOf` is not portable (depends on AD vs OpenLDAP and overlay configuration); a group search is. A `LDAP_USE_MEMBEROF=true` shortcut is a possible future addition, not in v1.

### Mock login (`AUTH_METHOD=mock`)

Identical to LDAP from the user's perspective — same form, same CSRF handling, same routes — but `complete()` just compares against `MOCK_USERNAME` / `MOCK_PASSWORD` and returns a `User` built from `MOCK_GROUPS` / `MOCK_DISPLAY_NAME`. The mock walks the exact same routes and middleware as production.

### Authenticated-request path

```python
async def _current_user(
    request: Request,
    store: SessionStore = Depends(get_session_store),
    settings: AuthSettings = Depends(get_auth_settings),
) -> User:
    sid = (
        request.cookies.get(settings.cookie_name)
        or _bearer(request.headers.get("authorization"))
    )
    if not sid:
        raise _unauthenticated(request)
    session = await store.get_and_refresh(sid)
    if session is None:
        raise _unauthenticated(request)
    return session.user

CurrentUser = Annotated[User, Depends(_current_user)]
OptionalCurrentUser = Annotated[User | None, Depends(_optional_current_user)]
```

`_unauthenticated` raises `AuthRequired`. The exception handler turns this into:
- `Accept: text/html` ⇒ `RedirectResponse(f"/login?next={path}", 302)`, cookie cleared.
- otherwise ⇒ `Response(status_code=401)`.

The cookie's `Max-Age` is **not** rotated on every response — only the server-side TTL is sliding. Rationale: rotating cookies on every response means writing `Set-Cookie` on every HTML page and every Datastar SSE response, the latter being impossible mid-stream. With a 12 h TTL, the worst-case "user came back after 11.99 h" still re-arms the server-side session for another 12 h.

### Group guard

```python
def require_group(*groups: str):
    async def _check(user: CurrentUser) -> User:
        if not set(groups) & set(user.groups):
            raise AuthForbidden(needed=groups, have=user.groups)
        return user
    return _check
```

Used as `Depends(require_group("admins"))`. `AuthForbidden` returns `403`:
- HTML routes: rendered `templates/auth/forbidden.html` showing required vs. actual groups.
- API routes: bare `403`.

We do **not** redirect on 403 — the user is authenticated; redirecting to `/login` would imply auth failure rather than authorization failure.

### Logout (`POST /logout`)

```python
@router.post("/logout")
async def logout(
    user: CurrentUser,
    store: SessionStore = Depends(get_session_store),
    settings: AuthSettings = Depends(get_auth_settings),
    csrf: None = Depends(verify_csrf_form),
):
    sid = ...    # extracted by the same code as _current_user
    await store.delete(sid)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.cookie_name)
    return response
```

`303 See Other` (not `302`) so the browser switches `POST` to `GET` for the redirect. CSRF check is the same double-submit cookie pattern used by `/login`.

---

## Error handling

| Failure | Where | Response |
|---|---|---|
| OAuth state mismatch (CSRF or stale cookie) | `/login/callback` | 302 to `/login?error=oauth_state` |
| OAuth code exchange fails | `/login/callback` | 302 to `/login?error=oauth_exchange` |
| OIDC discovery unreachable | App startup | App fails to start with a clear log line |
| OAuth callback returns no `groups` claim | `/login/callback` | Login succeeds with empty `groups`; WARN logged once per session |
| LDAP bind fails (bad creds) | `/login` POST | 302 to `/login?error=invalid_credentials` |
| LDAP server unreachable | `/login` POST | 302 to `/login?error=ldap_unreachable`; full error logged server-side |
| LDAP group search fails | `/login` POST | 302 to `/login?error=ldap_groups`; no half-authenticated session created |
| Mock provider creds mismatch | `/login` POST | 302 to `/login?error=invalid_credentials` |
| CSRF mismatch on login form | `/login` POST | 400 with a minimal HTML page; no redirect (would loop) |
| `AuthForbidden` (logged-in user lacks group) | any route | HTML: 403 forbidden.html. API: 403 empty body |

The error template renders `?error=<token>` as a human-readable message. The query token is stable; the message lives in the template.

### Session expiry

| Where | Behavior |
|---|---|
| Cookie `Max-Age` reached browser-side | Browser stops sending the cookie. Next request looks unauthenticated. Server entry evicted lazily on next attempted use. |
| Server-side `expires_at` reached, request comes in | `get_and_refresh` deletes and returns `None`. Same outcome as no cookie. |
| Mid-stream on `/api/clock` | Stream keeps running (auth checked at request start). Stream ends on disconnect or restart. |

### Logging

- INFO on successful login: `auth: login user=<display_name> subject=<...> method=<...> groups=[...]`
- INFO on logout: `auth: logout user=<display_name> subject=<...>`
- WARNING on every error case in the table above, with full server-side detail.

---

## Existing routes — what gets gated

| Route | Auth requirement | Notes |
|---|---|---|
| `GET /` | `CurrentUser` | Renders `index.html`. Unauthenticated → 302 /login |
| `GET /api/greet` | `CurrentUser` | Existing demo route, now protected |
| `GET /api/clock` | `CurrentUser` | Existing demo route, now protected |
| `POST /logout` | `CurrentUser` + CSRF | New |
| `GET /login`, `GET /login/callback`, `POST /login` | unauthenticated | New |
| `GET /api/whoami` | `CurrentUser` | New; returns `{"subject", "display_name", "groups"}` as JSON |

The `index.html` template gets a small change: header reads `Iris × Datastar — logged in as {{ user.display_name }}`, and a logout button (`<form method="post" action="/logout">` with the CSRF hidden field) is added.

---

## Testing strategy

### Default mode

Tests boot the app under `AUTH_METHOD=mock`. Every route goes through the real auth middleware, the real `CurrentUser` dep, the real session store. The mock provider is the only differing component. No bypass middleware.

### Fixtures

```python
# tests/conftest.py

@pytest.fixture
def app():
    """App configured with AUTH_METHOD=mock; default mock creds alice/secret, groups (admins, users)."""
    os.environ.update({
        "AUTH_METHOD": "mock",
        "MOCK_USERNAME": "alice",
        "MOCK_PASSWORD": "secret",
        "MOCK_GROUPS": "admins,users",
        "MOCK_DISPLAY_NAME": "Alice",
        "COOKIE_SECURE": "false",
    })
    from iris.app import build_app
    return build_app()

@pytest.fixture
def client(app):
    return TestClient(app)

@pytest.fixture
def authed_client(app):
    """Pre-creates a session in the store directly. For feature tests that just need a logged-in user."""
    c = TestClient(app)
    sid = _seed_session(app, user=User(subject="alice", display_name="Alice", groups=("admins", "users")))
    c.cookies.set("iris_session", sid)
    return c
```

`build_app()` is a new factory in `iris.app` that constructs a fresh `FastAPI` instance per call (replaces the module-level `app` for testability; the module still re-exports a process-wide `app = build_app()` for `uvicorn iris.app:app`).

### Test buckets

```
tests/auth/
├── test_session_store.py    # create/get/delete/expiry, lazy eviction, sliding refresh
├── test_csrf.py             # double-submit happy path & mismatch
├── test_login_mock.py       # full mock login + /api/whoami round-trip
├── test_login_ldap.py       # ldap3 MOCK_SYNC strategy with canned directory
├── test_login_oauth.py      # httpx MockTransport mocking OIDC discovery + token + userinfo
├── test_logout.py           # CSRF-required, deletes session, clears cookie
├── test_deps.py             # CurrentUser, OptionalCurrentUser, require_group(...)
└── test_error_pages.py      # ?error= rendering, forbidden.html

tests/test_app.py            # existing — switch to authed_client; existing assertions unchanged
```

### Test isolation for external systems

- **LDAP**: `ldap3.MOCK_SYNC` strategy with an in-memory directory built in setup. No real LDAP server.
- **OAuth**: `httpx.MockTransport` mocking the discovery URL, token endpoint, and userinfo endpoint. Authlib uses httpx underneath, so injecting at the transport level is sufficient. PKCE and state generation get a fixed-seed `secrets` patch for deterministic assertions.

`uv run pytest` stays sub-second; no external services touched.

### Carryover footgun

Infinite SSE streams (`/api/clock`) deadlock the sync `TestClient`. The existing workaround stays: unit-test the generator (`_clock_stream`) directly, not via the route. Auth doesn't change this.

---

## Dependencies to add

| Package | Use | Notes |
|---|---|---|
| `authlib` | OIDC client | Discovery, token exchange, JWT validation, signed-cookie helper |
| `ldap3` | LDAP client | Pure Python, no C deps; ships `MOCK_SYNC` test strategy |
| `itsdangerous` | Already pulled by Starlette | Used for the `oauth_state` signed cookie if Authlib's signer isn't reachable |

No new dev deps; existing `pytest` + `httpx` cover the test approach above.

---

## Risks & open follow-ups

1. **Empty `groups` claim from Keycloak.** Most common misconfig; we log a WARNING but the user still gets in. Could be tightened to "fail closed if groups are missing" via an env flag in v1.1.
2. **No mid-stream session expiry handling for Datastar.** Acceptable for v1; v1.1 could add a `datastar-patch-elements` toast injecting "your session expired".
3. **No SAML.** Out of scope for v1. Adding it would be a new file under `providers/`.
4. **No persistent sessions.** ≤ 20 users + restart-acceptable means in-memory is fine. If user count grows or restarts become disruptive, swap `InMemorySessionStore` for a Redis-backed implementation. The store API is small enough that this is a focused change.
