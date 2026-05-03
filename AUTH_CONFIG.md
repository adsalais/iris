# AUTH_CONFIG.md

Practical guide to configuring authentication and authorization in **iris**, including:

1. How to use the `iris.auth` API in routes and templates.
2. How to author the `authz.yaml` role mapping.
3. How to run a local **OpenLDAP** container for the LDAP provider.
4. How to run a local **Keycloak** container for the OAuth/OIDC provider.
5. How to point iris at each backend.
6. How each provider's login flow actually works on the wire (ASCII sequence diagrams).
7. The security model — what iris protects, how, and the residual risks.

This document is example-driven and copy-paste ready. For internal design notes, see `CLAUDE.md`.

---

## 1. Using authentication in your code

### 1.1 The public API

Everything you need is re-exported from `iris.auth`:

```python
from iris.auth import (
    CurrentUser,            # User; 401 if no session
    OptionalCurrentUser,    # User | None; never raises
    CurrentSession,         # UserSession (id, created_at, expires_at, user, data)
    SessionData,            # the per-session mutable dict
    CurrentRoles,           # frozenset[str] of effective role names
    require_role,           # dependency factory: Depends(require_role("admin"))
    User, UserSession,
    install,                # wires routes/handlers/store onto a FastAPI app
)
```

`install(app)` is already called inside `build_app()` in `src/iris/app.py`, so a freshly-built app comes with `/login`, `/login/callback`, `/logout`, and `/api/whoami` plus the exception handlers and the in-memory session store.

### 1.2 Guarding routes

```python
from fastapi import Depends, FastAPI
from iris.auth import CurrentUser, OptionalCurrentUser, CurrentRoles, require_role, User

app = FastAPI()

@app.get("/me")
async def me(user: CurrentUser):
    # 401 (redirect to /login for HTML, JSON 401 for API) if not signed in.
    return {"username": user.username, "groups": list(user.groups)}

@app.get("/")
async def home(user: OptionalCurrentUser):
    # Public route — `user` is None for anonymous visitors.
    return {"signed_in": user is not None}

@app.get("/admin", dependencies=[Depends(require_role("admin"))])
async def admin_index():
    # 403 unless the caller's effective roles include "admin".
    return {"ok": True}

@app.get("/docs/list")
async def list_docs(user: User = Depends(require_role("reader"))):
    # `require_role` returns the User, so you can also use it as a regular dep.
    return {"viewer": user.display_name}

@app.get("/api/whoami")
async def whoami(user: CurrentUser, roles: CurrentRoles):
    return {"user": user.username, "roles": sorted(roles)}
```

`require_role("foo")` admits any user whose effective role set contains `foo` directly **or via the `includes:` graph** in `authz.yaml`. If `foo` isn't defined in the YAML, the request returns **500** (operator typo, not a permission denial) and the missing name is logged server-side.

### 1.3 Per-session data

Each session carries a mutable `dict` you can stash arbitrary state in:

```python
from iris.auth import SessionData, CurrentSession

@app.post("/draft")
async def save_draft(data: SessionData, body: dict):
    data["draft"] = body          # in-memory; survives until logout/expiry
    return {"ok": True}

@app.get("/me/full")
async def me_full(s: CurrentSession):
    return {"id": s.id, "since": s.created_at.isoformat(), "keys": list(s.data)}
```

State is wiped on logout, expiry, or process restart. Don't rely on it for anything that needs to survive a redeploy.

### 1.4 Templates

The user object is available to any Jinja template via `request.state.user` (set by the auth middleware), so a base template can do:

```jinja
{% if request.state.user %}
  Hi, {{ request.state.user.display_name }} —
  <form method="post" action="/logout">
    <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
    <button type="submit">Log out</button>
  </form>
{% else %}
  <a href="/login?next={{ request.url.path | urlencode }}">Sign in</a>
{% endif %}
```

`POST /logout` is CSRF-required — render the token from `iris.auth.csrf.issue_csrf_token` (already done in `routes.py` for `/login`).

---

## 2. Authoring `authz.yaml`

The role mapping lives in the file pointed to by `AUTHZ_CONFIG_PATH`. Application code only ever names **internal roles** (`admin`, `writer`, `reader`); the YAML maps those to the IdP-supplied groups and usernames. Operators can re-edit the file without a redeploy — the loader picks up changes on the next request.

### 2.1 Schema

```yaml
roles:
  reader:
    groups: []
    users: []

  writer:
    groups: ["editors"]              # external IdP group names
    users: ["bob"]                   # external usernames (case-insensitive)
    includes: ["reader"]             # writers also have reader's permissions

  admin:
    groups: ["ldap-admins", "platform-team"]
    users: ["alice"]
    includes: ["writer"]             # admins transitively get writer + reader
```

Rules:

- Top-level must be exactly `roles:`. No other keys are accepted.
- Per-role keys are limited to `{groups, users, includes}`; all default to `[]`.
- Role names match `[a-zA-Z0-9_-]+`.
- `includes:` references must be defined roles. Cycles are rejected.
- Duplicate role keys are rejected (a custom YAML loader catches them; PyYAML's default would silently overwrite).
- `groups:` matches against `User.groups` **case-sensitively** (verbatim from the IdP).
- `users:` matches `User.username` **case-insensitively**.

### 2.2 Where `User.username` comes from

| Provider | Source of `username`                                                                                       |
| -------- | ---------------------------------------------------------------------------------------------------------- |
| OAuth    | `preferred_username` claim, falling back to `sub` (the IdP's opaque subject id)                             |
| LDAP     | The form-submitted username, the same value substituted into `LDAP_BIND_DN_TEMPLATE`                       |
| Mock     | `MOCK_USERNAME`                                                                                            |

If your OIDC IdP doesn't issue `preferred_username`, your `users:` lists must contain the `sub` UUIDs.

### 2.3 Live reload and bad edits

The loader stats the file on every request and reparses on mtime change. If a save produces an invalid file (syntax error, schema error, undefined include, cycle), the loader logs an `ERROR` and **keeps serving the last-known-good mapping** — your app keeps working until you fix the file.

A bad **initial** file at boot is *not* protected: the app refuses to start. This is intentional (consistent with the rest of iris's "fail-loud at boot" stance).

### 2.4 Example file for the dev setups in this doc

```yaml
# authz.yaml
roles:
  reader:
    groups: []
    users: []

  writer:
    groups: ["editors"]
    includes: ["reader"]

  admin:
    groups: ["admins", "platform-team"]
    users: ["alice"]
    includes: ["writer"]
```

Save as `./authz.yaml` and set `AUTHZ_CONFIG_PATH=./authz.yaml` in your `.env`.

---

## 3. Local OpenLDAP via Docker

Goal: a throwaway LDAP server populated with users + groups so you can drive iris's LDAP provider without a corporate directory. We'll use **`bitnami/openldap`** because it builds the directory tree from environment variables — no LDIF authoring required.

### 3.1 The compose file

Save as `docker/ldap/docker-compose.yml`:

```yaml
services:
  ldap:
    image: bitnami/openldap:2.6
    container_name: iris-ldap
    ports:
      - "1389:1389"          # plaintext LDAP (dev only)
    environment:
      LDAP_ROOT: "dc=corp,dc=local"
      LDAP_ADMIN_USERNAME: "admin"
      LDAP_ADMIN_PASSWORD: "adminpass"

      # Users — created under ou=users,dc=corp,dc=local with objectClass inetOrgPerson.
      # Each entry pairs a username with a password (same index).
      LDAP_USERS: "alice,bob,carol"
      LDAP_PASSWORDS: "alicepass,bobpass,carolpass"

      # Groups — created under ou=users,dc=corp,dc=local (Bitnami default) as
      # groupOfNames entries with `member` attributes pointing at the user DNs.
      LDAP_GROUP: "admins"
      LDAP_EXTRA_SCHEMAS: "cosine,inetorgperson,nis"

      # Allow the admin to read members; iris re-binds as the user, so this is
      # for `ldapsearch` debugging only.
      LDAP_ALLOW_ANON_BINDING: "no"
    volumes:
      - ldap_data:/bitnami/openldap

volumes:
  ldap_data:
```

Bring it up:

```bash
docker compose -f docker/ldap/docker-compose.yml up -d
```

### 3.2 Adding more groups and a non-admin user

`bitnami/openldap` only exposes a single `LDAP_GROUP` env var, so to wire `bob` into a separate `editors` group we add an LDIF as a one-shot. Save as `docker/ldap/extra.ldif`:

```ldif
dn: cn=editors,ou=users,dc=corp,dc=local
objectClass: groupOfNames
cn: editors
member: cn=bob,ou=users,dc=corp,dc=local

dn: cn=platform-team,ou=users,dc=corp,dc=local
objectClass: groupOfNames
cn: platform-team
member: cn=alice,ou=users,dc=corp,dc=local
```

Apply it once the container is healthy:

```bash
docker exec -i iris-ldap ldapadd \
  -x -H ldap://localhost:1389 \
  -D "cn=admin,dc=corp,dc=local" -w adminpass \
  < docker/ldap/extra.ldif
```

### 3.3 Sanity-check from the host

```bash
# All entries under the base
docker exec iris-ldap ldapsearch -x -H ldap://localhost:1389 \
  -D "cn=admin,dc=corp,dc=local" -w adminpass \
  -b "dc=corp,dc=local" "(objectClass=*)" dn

# Groups Alice belongs to (the same query iris runs)
docker exec iris-ldap ldapsearch -x -H ldap://localhost:1389 \
  -D "cn=admin,dc=corp,dc=local" -w adminpass \
  -b "ou=users,dc=corp,dc=local" \
  "(member=cn=alice,ou=users,dc=corp,dc=local)" cn
```

Expected: `admins` and `platform-team` for alice; `editors` for bob.

### 3.4 Pointing iris at it

In `.env`:

```bash
AUTH_METHOD=ldap

LDAP_URL=ldap://127.0.0.1:1389
LDAP_BIND_DN_TEMPLATE=cn={username},ou=users,dc=corp,dc=local
LDAP_GROUP_BASE_DN=ou=users,dc=corp,dc=local

# Plaintext is acceptable on localhost for dev. iris refuses ldap:// by default.
LDAP_REQUIRE_TLS=false

COOKIE_SECURE=false
AUTHZ_CONFIG_PATH=./authz.yaml
```

Notes:

- The bind DN uses `cn={username}` because `bitnami/openldap` provisions users under `cn=...`, not `uid=...`. For a real OpenLDAP/389DS deployment, `uid={username},ou=people,dc=corp,dc=local` is more typical — match whatever your directory uses.
- `LDAP_REQUIRE_TLS=true` (the default) rejects non-`ldaps://` URLs at startup. Only flip it off for local dev.
- iris reads groups via `(member=<bind_dn>)` against `LDAP_GROUP_BASE_DN`, so groups must use the `groupOfNames` objectClass with `member` attributes — which is what the LDIF above does.

Start iris:

```bash
uv run iris
# open http://127.0.0.1:8000/ — you'll be redirected to /login
# username: alice    password: alicepass     → groups: admins, platform-team
# username: bob      password: bobpass       → groups: editors
```

With the `authz.yaml` from §2.4, alice gets `admin → writer → reader` and bob gets `writer → reader`.

---

## 4. Local Keycloak via Docker (OAuth / OIDC)

Goal: an OIDC IdP iris can do code-flow + PKCE against, with a couple of users in real groups. We'll use the **`quay.io/keycloak/keycloak`** dev container.

### 4.1 The compose file

Save as `docker/keycloak/docker-compose.yml`:

```yaml
services:
  keycloak:
    image: quay.io/keycloak/keycloak:24.0
    container_name: iris-keycloak
    command: ["start-dev", "--import-realm"]
    environment:
      KEYCLOAK_ADMIN: admin
      KEYCLOAK_ADMIN_PASSWORD: adminpass
      KC_HEALTH_ENABLED: "true"
    ports:
      - "8080:8080"
    volumes:
      - ./realm-iris.json:/opt/keycloak/data/import/realm-iris.json:ro
      - keycloak_data:/opt/keycloak/data

volumes:
  keycloak_data:
```

### 4.2 The realm export

Save as `docker/keycloak/realm-iris.json`. It defines:

- A realm called `iris`.
- A confidential client `iris` with PKCE enforced and the iris callback URL whitelisted.
- A `groups` client scope that injects the `groups` claim into the userinfo response (iris reads `User.groups` from this claim).
- Users `alice` and `bob` with passwords, and groups `admins`, `editors`, `platform-team`.

```json
{
  "realm": "iris",
  "enabled": true,
  "groups": [
    { "name": "admins" },
    { "name": "editors" },
    { "name": "platform-team" }
  ],
  "clientScopes": [
    {
      "name": "groups",
      "protocol": "openid-connect",
      "attributes": { "include.in.token.scope": "true", "display.on.consent.screen": "false" },
      "protocolMappers": [
        {
          "name": "groups",
          "protocol": "openid-connect",
          "protocolMapper": "oidc-group-membership-mapper",
          "config": {
            "claim.name": "groups",
            "full.path": "false",
            "id.token.claim": "true",
            "access.token.claim": "true",
            "userinfo.token.claim": "true"
          }
        }
      ]
    }
  ],
  "clients": [
    {
      "clientId": "iris",
      "secret": "iris-dev-secret",
      "enabled": true,
      "publicClient": false,
      "standardFlowEnabled": true,
      "directAccessGrantsEnabled": false,
      "redirectUris": ["http://127.0.0.1:8000/login/callback"],
      "webOrigins": ["http://127.0.0.1:8000"],
      "attributes": {
        "pkce.code.challenge.method": "S256"
      },
      "defaultClientScopes": ["openid", "profile", "email", "groups"],
      "optionalClientScopes": []
    }
  ],
  "users": [
    {
      "username": "alice",
      "enabled": true,
      "email": "alice@example.com",
      "firstName": "Alice",
      "lastName": "A.",
      "credentials": [{ "type": "password", "value": "alicepass", "temporary": false }],
      "groups": ["/admins", "/platform-team"]
    },
    {
      "username": "bob",
      "enabled": true,
      "email": "bob@example.com",
      "firstName": "Bob",
      "lastName": "B.",
      "credentials": [{ "type": "password", "value": "bobpass", "temporary": false }],
      "groups": ["/editors"]
    }
  ]
}
```

Bring it up:

```bash
docker compose -f docker/keycloak/docker-compose.yml up -d
# Wait ~10s for the import to finish, then:
curl -s http://127.0.0.1:8080/realms/iris/.well-known/openid-configuration | head
```

The discovery URL works → realm is loaded.

### 4.3 Pointing iris at it

In `.env`:

```bash
AUTH_METHOD=oauth

OIDC_ISSUER_URL=http://127.0.0.1:8080/realms/iris
OIDC_CLIENT_ID=iris
OIDC_CLIENT_SECRET=iris-dev-secret
OIDC_SCOPES=openid profile email groups

COOKIE_SECURE=false
AUTHZ_CONFIG_PATH=./authz.yaml
```

Start iris and visit `http://127.0.0.1:8000/`. The `/login` link redirects to Keycloak; after sign-in you bounce back to iris with a session cookie.

Notes:

- Use `127.0.0.1` (not `localhost`) consistently for both iris and Keycloak in URLs — mixed hosts can cause same-site cookie weirdness in some browsers during dev.
- iris caches the IdP's JWKS once per process. If you rotate Keycloak signing keys, restart iris.
- The `groups` claim arrives without a leading `/` because the mapper has `full.path: false`. So `User.groups` will be `("admins", "platform-team")` for alice, matching the YAML in §2.4.

---

## 5. Switching back to the mock provider

For a dependency-free smoke test (also what the test suite uses), point everything at the mock provider:

```bash
AUTH_METHOD=mock
MOCK_USERNAME=alice
MOCK_PASSWORD=secret
MOCK_GROUPS=admins,platform-team
MOCK_DISPLAY_NAME=Alice

COOKIE_SECURE=false
AUTHZ_CONFIG_PATH=./authz.yaml
```

Same `authz.yaml` works — alice's `admins` group resolves to the `admin` role (and transitively to `writer` and `reader`).

---

## 6. Connection flows

All three providers share the same shape: an unauthenticated request hits a protected route, gets a 302 to `/login?next=...`, and ends up with an `iris_session` cookie. What differs is how the credential check happens between `GET /login` and the moment the session is created.

### 6.1 Mock provider

A single round-trip — the form POST is itself the credential check.

```
Browser                       iris (FastAPI)
   │                              │
   │ GET /admin                   │
   ├─────────────────────────────▶│  AuthRequired
   │ 302 /login?next=/admin       │
   │◀─────────────────────────────┤
   │ GET /login?next=/admin       │
   ├─────────────────────────────▶│  MockProvider.begin
   │ 200 (HTML form               │  → render form, mint csrf
   │      Set-Cookie: iris_csrf)  │
   │◀─────────────────────────────┤
   │ POST /login                  │
   │   form: username,password,   │
   │         csrf_token, next     │
   │   cookie: iris_csrf          │
   ├─────────────────────────────▶│  TokenBucket.check (10 burst, 0.2/s)
   │                              │  verify_csrf_form
   │                              │  MockProvider.authenticate:
   │                              │    constant-time compare to
   │                              │    MOCK_USERNAME / MOCK_PASSWORD
   │                              │  → User(groups=MOCK_GROUPS)
   │                              │  store.create(user)
   │ 302 /admin                   │
   │   Set-Cookie: iris_session   │
   │   Set-Cookie: iris_csrf=     │
   │     (cleared on success)     │
   │◀─────────────────────────────┤
   │ GET /admin                   │
   │   cookie: iris_session       │
   ├─────────────────────────────▶│  resolve session → require_role
   │ 200                          │
   │◀─────────────────────────────┤
```

### 6.2 LDAP provider

The LDAP **bind** is the password check; iris never re-handles the password. Group membership is then resolved with a portable `(member=<bind_dn>)` search.

```
Browser              iris                          OpenLDAP
   │                  │                                │
   │ GET /login       │                                │
   ├─────────────────▶│  render form + csrf            │
   │                  │                                │
   │ POST /login      │                                │
   │  user,pass,csrf  │                                │
   ├─────────────────▶│  verify_csrf_form              │
   │                  │  username regex check          │
   │                  │  bind_dn = LDAP_BIND_DN_       │
   │                  │    TEMPLATE.format(username)   │
   │                  ├──── BIND bind_dn,password ────▶│
   │                  │                                │  verify password
   │                  │◀──── BindResult: OK ───────────┤
   │                  │                                │
   │                  ├──── SEARCH bind_dn ───────────▶│  read cn (display name)
   │                  │◀──── entry ────────────────────┤
   │                  │                                │
   │                  ├──── SEARCH GROUP_BASE_DN ─────▶│
   │                  │  filter:                       │
   │                  │   (member=<escaped bind_dn>)   │
   │                  │  attrs: [cn]                   │
   │                  │◀──── group cns ────────────────┤
   │                  │                                │
   │                  │  User(subject=bind_dn,         │
   │                  │       username=<form>,         │
   │                  │       groups=cns)              │
   │                  │  store.create(user)            │
   │ 302 next         │                                │
   │  Set-Cookie:     │                                │
   │    iris_session  │                                │
   │◀─────────────────┤                                │
```

Bad password → `LDAPInvalidCredentialsResult` → 401 + redirect to `/login?error=invalid_credentials`. Server unreachable → `ldap_unreachable` token. Group read failure after a successful bind → `ldap_groups` (signals "your password worked but we can't list your groups; see an admin").

### 6.3 OAuth / OIDC provider

Three-leg authorization-code flow with PKCE S256 and CSRF state. iris keeps no server-side state between `/login` and `/login/callback` — the verifier travels in a signed cookie.

```
Browser                iris                       Keycloak
   │                    │                            │
   │ GET /login         │                            │
   ├───────────────────▶│  OAuthProvider.begin       │
   │                    │  _ensure_discovered:       │
   │                    ├──── GET .well-known ─────▶ │
   │                    │◀──── doc + jwks_uri ────── │
   │                    ├──── GET jwks_uri ────────▶ │
   │                    │◀──── JWKS ─────────────────│
   │                    │  (cached for process life) │
   │                    │                            │
   │                    │  state    = random(16)     │
   │                    │  verifier = random(64)     │
   │                    │  challenge= S256(verifier) │
   │                    │  sign {state,verifier,next}│
   │                    │    → oauth_state cookie    │
   │ 302 <authorize>?   │                            │
   │  client_id,state,  │                            │
   │  code_challenge,…  │                            │
   │  Set-Cookie:       │                            │
   │    oauth_state     │                            │
   │◀───────────────────┤                            │
   │                    │                            │
   │ GET <authorize>… ──────────────────────────────▶│
   │                    │                            │  user signs in
   │ 302 /login/callback?code=...&state=...          │
   │◀────────────────────────────────────────────────│
   │                    │                            │
   │ GET /login/callback│                            │
   │   ?code,state      │                            │
   │   cookie:          │                            │
   │     oauth_state    │                            │
   ├───────────────────▶│  OAuthProvider.complete    │
   │                    │  unsign oauth_state cookie │
   │                    │  state ?= query.state      │
   │                    │                            │
   │                    ├──── POST <token> ─────────▶│
   │                    │  grant_type=authorization_ │
   │                    │    code, code, verifier,   │
   │                    │    client_id, secret       │
   │                    │◀──── {access_token,        │
   │                    │       id_token} ───────────│
   │                    │                            │
   │                    │  verify id_token:          │
   │                    │   sig vs JWKS[kid]         │
   │                    │   iss, aud=client_id, exp  │
   │                    │                            │
   │                    ├──── GET <userinfo> ───────▶│
   │                    │   Authorization: Bearer    │
   │                    │◀──── {sub, name,           │
   │                    │       preferred_username,  │
   │                    │       groups[…]} ──────────│
   │                    │                            │
   │                    │  User(subject=sub,         │
   │                    │       username=pref_user,  │
   │                    │       groups=groups)       │
   │                    │  store.create(user)        │
   │ 302 next           │                            │
   │  Set-Cookie:       │                            │
   │    iris_session    │                            │
   │  Set-Cookie:       │                            │
   │    oauth_state=    │                            │
   │    (cleared)       │                            │
   │◀───────────────────┤                            │
```

Discovery is **lazy**: the first `/login` after restart pays the latency to fetch `.well-known/...` and the JWKS. Subsequent logins reuse the cached endpoints.

---

## 7. Security model

What iris actually does, what it doesn't do, and where the residual risks are.

### 7.1 Session cookies

- `iris_session` is a 256-bit random opaque id. `HttpOnly`, `SameSite=Lax`, `Path=/`. `Secure` flag controlled by `COOKIE_SECURE` (default `true`).
- Sliding TTL (`SESSION_TTL_SECONDS`, 12h default) refreshes on every authenticated request.
- Absolute cap (`SESSION_ABSOLUTE_TTL_SECONDS`, 30d default) — even active sessions must re-authenticate eventually.
- Per-user cap (`SESSION_MAX_PER_USER`, 10 default) — the eleventh concurrent login evicts the oldest. Limits damage if one cookie is stolen.
- Storage is in-process (`InMemorySessionStore`); a redeploy invalidates all sessions. **Multi-worker silently breaks sessions** — keep `--workers 1` until the Redis-backed store lands.

### 7.2 CSRF

`POST /login` and `POST /logout` use a double-submit pattern:

- Server mints a token, sends it both as `iris_csrf` cookie and as a hidden `csrf_token` form field.
- Route requires the two values to match; mismatch → 403.
- **Token rotation on login:** on successful `POST /login` (and OAuth callback), the `iris_csrf` cookie is cleared, so any pre-auth token a phisher captured is dead the moment the user signs in. The next form render mints a fresh token.

For OAuth, the `state` parameter plays the same role across the IdP round-trip. It lives in the HMAC-signed `oauth_state` cookie (10-minute TTL) along with the PKCE verifier and the original `next`.

### 7.3 Rate limiting

`POST /login` keys on `request.client.host` via an in-process token bucket: capacity 10, refill 0.2/sec → 10-attempt burst then ~12/min sustained. Exhausted clients get **429** with a `Retry-After` header.

Caveat: behind a reverse proxy, `client.host` is the *proxy's* IP and the bucket becomes effectively global. Run uvicorn with `--proxy-headers --forwarded-allow-ips=<proxy-ip>` so `X-Forwarded-For` is honored.

### 7.4 Open-redirect protection

`_safe_next(url)` accepts only same-origin **relative** paths. It rejects:

- empty / falsy strings,
- anything not starting with `/`,
- `//`-prefixed (protocol-relative) URLs,
- absolute URLs,
- backslash-containing strings (browsers normalize `\` → `/` *after* same-origin checks).

Applied at `POST /login` and `GET /login/callback`. Error-redirect URLs are built with `urllib.parse.urlencode` so error tokens can't break query parsing.

### 7.5 LDAP hardening

- `username` is regex-restricted (`[A-Za-z0-9._-]{1,64}`) **before** interpolation into `LDAP_BIND_DN_TEMPLATE`. Special characters cannot escape the DN.
- The `(member=<bind_dn>)` filter passes the bind DN through `ldap3.utils.conv.escape_filter_chars`, blocking filter-injection via crafted DNs.
- Anonymous bind is not a code path. Every authentication is a user-bind with the submitted password; a wrong password produces `LDAPInvalidCredentialsResult` and the request is rejected.
- `LDAP_REQUIRE_TLS=true` (default) refuses non-`ldaps://` URLs at startup. v1 has no StartTLS; use `ldaps://` everywhere except trusted-dev loopback.

### 7.6 OIDC hardening

- **PKCE S256** is mandatory on every `/login`. The verifier never goes over the wire — it travels client-to-iris-to-iris in the signed `oauth_state` cookie.
- `state` is a 16-byte URL-safe random. Mismatch on callback → 401.
- `id_token` verification: signature against JWKS by `kid`; `iss == OIDC_ISSUER_URL`; `aud == OIDC_CLIENT_ID`; `exp` not in the past. Algorithms are restricted to `RS256`/`ES256`.
- The `redirect_uri` is whitelisted at the IdP (`redirectUris` in the realm) — Keycloak refuses unknown URIs, so even a stolen `client_secret` can't redirect a victim through iris to an attacker-controlled callback.
- JWKS is fetched once and cached for the process lifetime. IdP signing-key rotation requires an iris restart. Acceptable for ≤20-user / multi-month rotation cadence; tighten by re-fetching on `kid`-not-in-set if rotation matters.

### 7.7 Authorization fail-loud

- `require_role("foo")` where `foo` is not defined in `authz.yaml` returns **500** with a generic body. The missing name is logged server-side but never returned. This catches operator typos like `require_role("reder")` instead of silently 403'ing every reader request.
- A bad **initial** YAML aborts boot. A bad **live** edit is logged at `ERROR` and the loader keeps serving the previous good mapping — so a 3am typo doesn't take prod down.
- `groups:` matches case-sensitively (verbatim from the IdP). `users:` matches case-insensitively. Get this wrong and you'll silently 403 — `/api/whoami` shows the resolved roles for fast diagnosis.

### 7.8 Logout

`POST /logout` is CSRF-required. It deletes the session record and clears the cookie. It does **not** call the IdP's `end_session_endpoint`, so a Keycloak SSO session can survive the iris logout — a fresh `/login` may sign the user straight back in without prompting. If you need single-logout semantics, wire it up at the IdP layer (e.g. a Keycloak logout link in your nav).

### 7.9 Residual risks

Documented honestly so they aren't surprises later:

- **Session cookies are bearer tokens.** Over plaintext HTTP they can be lifted by a passive observer. Always set `COOKIE_SECURE=true` in production and serve over HTTPS.
- **Single-worker constraint.** `--workers 1` is mandatory until the Redis-backed store lands. Multi-worker silently breaks sessions and rate limiting both.
- **Per-IP rate limit only.** A botnet hitting `/login` from many IPs gets one budget per IP. Pair with a WAF/Cloudflare/etc. if exposed to the open internet.
- **No JWKS rotation.** Restart iris after IdP key rotation.
- **Mock is a static credential.** Don't ship `AUTH_METHOD=mock` outside dev/test — the test suite locks it on, but production deploys must pin `AUTH_METHOD=oauth` or `ldap` explicitly.
- **`.env` may hold secrets.** `chmod 600 .env`; verify your container build doesn't bake it into images.

---

## 8. Troubleshooting cheatsheet

| Symptom                                              | Likely cause                                                                                                  |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Logged in but every request bounces back to `/login` | You're running `uvicorn --workers >1`. The session store is per-process. Use `--workers 1`.                   |
| `403 Forbidden` for a user you expected to admit     | Group name mismatch (case-sensitive) or username mismatch. Hit `/api/whoami` to see the resolved roles.       |
| `500` on a guarded route                             | `require_role("...")` names a role not in `authz.yaml`. Check server logs for the exact missing name.         |
| OIDC login dies with `oauth_discovery`               | The discovery URL is wrong, or Keycloak isn't reachable from iris. `curl <issuer>/.well-known/...` to verify. |
| OIDC login succeeds but `groups` is empty            | The `groups` client scope isn't a *default* scope for the `iris` client, or the mapper isn't mapping into userinfo. |
| LDAP login dies with `ldap_unreachable`              | Wrong port, wrong host, or `LDAP_REQUIRE_TLS=true` against an `ldap://` URL. Check `LDAP_URL` and the flag.   |
| LDAP login succeeds but no groups                    | `LDAP_GROUP_BASE_DN` is wrong, or your group entries don't have a `member` attribute pointing at the bind DN. |
| YAML edit silently doesn't take effect               | The loader logs `ERROR` and keeps the previous mapping when a save is invalid — check the iris logs.         |

---

## 9. Production reminders

- Set `COOKIE_SECURE=true` and serve over HTTPS.
- Set `LDAP_REQUIRE_TLS=true` and use `ldaps://` (or terminate TLS at a sidecar that exposes `ldaps://` to iris).
- Keep `AUTH_METHOD=mock` *out* of every non-test `.env`. The mock provider accepts a single hardcoded credential — it's a dev tool, not a fallback.
- `chmod 600 .env` on multi-tenant hosts; it can hold `OIDC_CLIENT_SECRET` and `MOCK_PASSWORD`.
- iris is `--workers 1` only for v1. If you need to scale out, swap `InMemorySessionStore` for a Redis-backed implementation (the surface area is `create` / `get_and_refresh` / `delete`).
