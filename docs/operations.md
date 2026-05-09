# Operations

Operator-facing concerns for deploying and running iris. See `CLAUDE.md` for a project overview, `docs/auth.md` for authentication/authorization internals, and `docs/clickhouse.md` for ClickHouse provisioning internals.

---

## Configuration

Env vars are loaded at `import iris` time via `python-dotenv`. If a `.env` file exists at the project root (gitignored), its values populate `os.environ` for any keys not already set. Real shell env vars take precedence (`load_dotenv` is called with `override=False`), so CI and production deployments can override individual values without editing `.env`. Tests inherit the same loader; `tests/conftest.py` sets `os.environ.setdefault(...)` defaults at module scope before iris is imported, so test runs always end up with `AUTH_METHOD=mock` regardless of what `.env` contains.

### Auth env vars

```
AUTH_METHOD=oauth | ldap | mock
SESSION_COOKIE_NAME=iris_session
SESSION_TTL_SECONDS=43200            # 12h, sliding TTL refreshed on each request
SESSION_ABSOLUTE_TTL_SECONDS=2592000 # 30d, hard cap on top of sliding TTL
SESSION_MAX_PER_USER=10              # cap concurrent sessions per user (oldest evicted)
AUTH_DB_PATH=./iris-auth.db          # SQLite file backing the session store; :memory: for tests
COOKIE_SECURE=true                   # set false for local dev over http
IRIS_TRUST_FORWARDED_FOR=false       # when true, rate-limit + audit log key on the leftmost
                                     # X-Forwarded-For IP. Requires a trusted upstream proxy
                                     # that strips client-supplied X-Forwarded-For.

# OAuth (OIDC discovery)
OIDC_ISSUER_URL=https://keycloak.example.com/realms/iris
OIDC_CLIENT_ID=iris
OIDC_CLIENT_SECRET=...
OIDC_SCOPES=openid profile email groups
OIDC_CA_CERT_PATH=                   # optional: PEM bundle for IdP cert validation (private CA)

# LDAP
LDAP_URL=ldaps://ldap.example.com:636
LDAP_BIND_DN_TEMPLATE=uid={username},ou=people,dc=corp,dc=local
LDAP_GROUP_BASE_DN=ou=groups,dc=corp,dc=local
LDAP_REQUIRE_TLS=true                # reject ldap:// at startup
LDAP_CA_CERT_PATH=                   # optional: PEM bundle for cert validation

# Mock (for tests; AUTH_METHOD=mock)
MOCK_USERNAME=alice
MOCK_PASSWORD=secret
MOCK_GROUPS=admins,users
MOCK_DISPLAY_NAME=Alice
```

`AuthSettings.from_env()` runs at app construction; missing required vars or unrecognized values fail loudly. `_get_bool` raises on typos (`COOKIE_SECURE=ture` is rejected, not silently false).

### ClickHouse env vars

```
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=iris_service          # CH login iris connects as
CLICKHOUSE_PASSWORD=replace-me
CLICKHOUSE_SECURE=true                # required: true = https, false = http
CLICKHOUSE_VERIFY=true                # required: true = verify TLS cert, false = skip verification
# CLICKHOUSE_CA_CERT_PATH=/etc/ssl/certs/ca-bundle.crt
```

`ClickHouseSettings.from_env()` validates everything at app construction — missing required vars, typo'd booleans, non-int ports, and bad identifier names all fail loudly.

### Bootstrap admin env vars

```
CLICKHOUSE_ADMIN_USER=               # IdP username of the bootstrap admin (e.g. alice)
CLICKHOUSE_ADMIN_GROUP=              # IdP group name of bootstrap admins (e.g. iris_admin)
```

At boot, `bootstrap_admin` (in `iris.clickhouse.bootstrap`) always creates the `iris_global_admin` sentinel role. If `CLICKHOUSE_ADMIN_USER=alice` is set and no `_USER`-suffixed role currently holds the admin marker (ROLE ADMIN+WGO at global scope), iris creates `alice_USER` with `GRANT ALL ON *.* WITH GRANT OPTION` plus `iris_global_admin` granted to it. If `CLICKHOUSE_ADMIN_GROUP=iris_admin` is set and no `_GRP`-suffixed role currently holds admin, iris creates `iris_admin_GRP` the same way. Both channels are independently idempotent. See `docs/clickhouse.md` for the full bootstrap behavior and the `derive_capabilities` detection logic.

Set `CLICKHOUSE_ADMIN_USER` to whatever value iris derives as `username` from the configured auth provider — for OAuth that is the `preferred_username` claim (falling back to `sub`), for LDAP the value substituted into `LDAP_BIND_DN_TEMPLATE`, for mock `MOCK_USERNAME`. See `docs/auth.md` § Identity matching.

If neither `CLICKHOUSE_ADMIN_USER` nor `CLICKHOUSE_ADMIN_GROUP` is set, `iris_global_admin` is created but no admin role is granted; add an admin manually via CH DDL or restart with one of the vars set.

### `.env` permissions

The `.env` file may contain secrets (`OIDC_CLIENT_SECRET`, `MOCK_PASSWORD`, `CLICKHOUSE_PASSWORD`, etc.). On a multi-user host, run `chmod 600 .env` so it is only readable by the iris service user. The file is gitignored; verify that your container or build pipeline does not bake it into images.

---

## Multi-worker deployment

Sessions live in a SQLite file. The store opens its connection in WAL mode (`PRAGMA journal_mode=WAL`) so concurrent readers do not block on a writer, and `PRAGMA synchronous=NORMAL` keeps writes cheap. Multiple uvicorn workers share state by pointing at the same `AUTH_DB_PATH` on local disk:

```
uvicorn iris.app:build_app --factory --workers 4
```

Workers can scale freely on a single host as long as the DB path is on local disk reachable by every worker. Cross-host deploys still need a shared filesystem or a different store backend.

Sessions survive process restarts — a redeploy does not log every user out.

Production launches via uvicorn factory mode so importing `build_app` is side-effect-free for tests:

```
uvicorn.run("iris.app:build_app", factory=True, ...)
```

---

## Open redirect protection

`_safe_next(url)` accepts only same-origin relative paths. It rejects:

- empty strings
- strings not starting with `/`
- protocol-relative URLs starting with `//`
- absolute URLs
- strings containing a backslash (browsers normalize `\` → `/` before same-origin checks)

Applied at `POST /login` and `GET /login/callback`. Failure-redirect URLs are constructed via `urllib.parse.urlencode` so error tokens or path components cannot break query parsing.

---

## Open security follow-ups

Accepted residual risks for the ≤20-user / `--workers 1` deploy profile. Revisit when scaling out or relocating behind a load balancer.

- **Rate limiting behind a reverse proxy.** Closed by `IRIS_TRUST_FORWARDED_FOR=true`, which makes `iris.auth.client_ip.client_ip` resolve the bucket key from the leftmost X-Forwarded-For entry. The trusted proxy MUST strip any client-supplied X-Forwarded-For before adding its own; otherwise an attacker can spoof the leftmost value and bypass per-IP rate limits. Spec: `docs/superpowers/specs/2026-05-09-security-hardening-design.md`.
- **Rate-limiter memory bound.** `TokenBucket` is now LRU-capped at 10 000 entries (~0.4 MB). Past that threshold, eviction is best-effort: an attacker controlling >10K unique IPs evicts legitimate users' buckets, giving themselves fresh capacity per IP rotation. Acceptable for ≤20-user single-host deployments; a real DDoS demands an upstream WAF.
- **JWKS rotation cache.** `OAuthProvider` caches the IdP's JWKS once on first discovery. If the IdP rotates signing keys, all logins fail until iris is restarted. Tighten by re-fetching on `kid`-not-in-set if rotation matters.
- **OIDC discovery latency.** Discovery is lazy: the first login attempt after restart pays the discovery latency. Acceptable for v1, but means a slow IdP shifts startup latency to a request boundary instead of failing loud at boot.
- **`derive_capabilities` query cost.** At login, `derive_capabilities` runs a small handful of CH queries (role-grants walk + grants enumeration). Sub-millisecond at ≤20-user scale; for higher request volumes, consider caching the effective role set per user with a CH version-column invalidation.
- **Out-of-band admin promotion.** A raw `GRANT ALL ON *.*` outside iris bootstrap grants the ROLE ADMIN+WGO marker, so `derive_capabilities` returns `is_admin=True`. However, wildcard row policies are keyed on the `iris_global_admin` sentinel role, not on ROLE ADMIN+WGO directly. An out-of-band admin can query tables but won't see rows on tables with restrictive policies unless `iris_global_admin` is also granted to their `_USER` role. Mitigation: after any manual `GRANT ALL`, also run `GRANT iris_global_admin TO <username>_USER`.

---

## Deferred

- **Connection pooling.** `clickhouse-connect`'s `Client` is per-process today. Multi-worker deploys would benefit from a shared connection pool rather than one client per worker.
- **Streaming `query_as_user`.** For routes that need to stream large result sets back through Datastar SSE without buffering the whole response in memory.
- **Datastar version refresh.** The bundle is vendored at `src/iris/static/datastar.js`. Bumping the Datastar version is a manual two-step: re-download `https://cdn.jsdelivr.net/gh/starfederation/datastar@<version>/bundles/datastar.js` over the vendored file, then commit. There is no automated check that the vendored bytes match a known-good upstream hash — review carefully on bump.

---

## Migration runbooks

Recent migrations each shipped with operator runbooks in their respective specs:

- `docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md` — wiping `AUTH_DB_PATH` and replacing the old `IRIS_BOOTSTRAP_USER` env var with `CLICKHOUSE_ADMIN_USER`.
- `docs/superpowers/specs/2026-05-08-session-as-handle-design.md` — no operator-facing change; pure internal refactor.
- `docs/superpowers/specs/2026-05-08-bootstrap-rework-design.md` — replacing `IRIS_BOOTSTRAP_USER` and the old `CLICKHOUSE_SERVICE_ADMIN_*` env vars with `CLICKHOUSE_ADMIN_USER` and `CLICKHOUSE_ADMIN_GROUP`.

### 0.1.x → next: auth module reshape

The `Rights` type was renamed to `Capabilities` and the SQLite session-store column `rights_json` was renamed to `capabilities_json`. There is no in-code migration; operators upgrading must:

1. Stop iris.
2. Delete the SQLite file at `AUTH_DB_PATH` (default `./iris-auth.db`) plus its `.db-wal` and `.db-shm` sidecars.
3. Start iris.

In-flight sessions are invalidated; users re-login. The 12 h sliding TTL means most sessions would have expired anyway. Spec: `docs/superpowers/specs/2026-05-09-auth-module-reshape-design.md`.
