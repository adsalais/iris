# Local deploy stack

Docker Compose that brings up a **Keycloak** (OIDC) + **ClickHouse** pair so
you can run iris locally against real backends. Data persists to a
configurable directory **outside** the project, so `docker compose down`
does not destroy your test users / databases.

## Prerequisites

- Docker + Docker Compose (v2; `docker compose ...` not `docker-compose ...`)
- `uv` (for running iris itself; see top-level README)
- ~2 GB free disk (most of it ClickHouse)

## First-time setup

The recommended path is `deploy/up.sh` — a small wrapper that declares the
required variables (`DATA_DIR`, Keycloak admin, ClickHouse service-admin)
at the top, creates the bind-mount target dirs (chown'd for the in-container
Keycloak uid), and invokes `docker compose`.

```bash
cd deploy/

# 1. Open up.sh and set DATA_DIR to an absolute path outside the project
#    tree. Tweak admin credentials if you want non-defaults.
$EDITOR up.sh

# 2. Boot the stack. First run pulls images and imports the seed realm
#    (~30s for Keycloak, ~10s for CH). The script forwards extra args to
#    docker compose, so subsequent operations also go through it (see
#    "Common operations" below).
./up.sh

# 3. Wait for both services to report healthy.
./up.sh ps
# Expect STATUS = "healthy" for both.
```

After this, `${DATA_DIR}/keycloak/` and `${DATA_DIR}/clickhouse/` exist and
contain all persistent state. `docker compose down` keeps them; deleting
them re-seeds from `keycloak/realm.json` next time.

If you'd rather not touch the checked-in script, copy it: `cp up.sh
up.local.sh && $EDITOR up.local.sh`. `up.local.sh` is gitignored.

### Or: plain `.env` instead

`up.sh` is just a launcher around the same env vars `docker compose`
already reads from `deploy/.env`. If you prefer that flow, copy
`.env.example` to `.env`, edit it, and run `docker compose up -d` directly.
`.env` is gitignored. Both styles work; pick one.

## Configure iris to talk to the local stack

In the **project root** (not in `deploy/`), create `.env`:

```ini
# Auth: OIDC against the local Keycloak.
AUTH_METHOD=oauth
OIDC_ISSUER_URL=http://localhost:8080/realms/iris
OIDC_CLIENT_ID=iris
OIDC_CLIENT_SECRET=iris-dev-secret
OIDC_SCOPES=openid profile email groups

# Cookies: HTTP-only is required for local dev (no TLS).
COOKIE_SECURE=false

# ClickHouse: HTTP (no TLS), service-admin credentials must match
# deploy/.env's CLICKHOUSE_USER / CLICKHOUSE_PASSWORD.
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=8123
CLICKHOUSE_USER=iris_admin
CLICKHOUSE_PASSWORD=change-me-please
CLICKHOUSE_SECURE=false
CLICKHOUSE_VERIFY=false

# Bootstrap admin: ONE of these designates the first iris admin.
# Either CLICKHOUSE_ADMIN_USER must equal a Keycloak username, OR
# CLICKHOUSE_ADMIN_GROUP must equal a Keycloak group name. The seed realm
# ships with alice in the iris_admins group, so either of these works:
CLICKHOUSE_ADMIN_USER=alice
CLICKHOUSE_ADMIN_GROUP=iris_admins
```

Then run iris from the project root:

```bash
uv run iris
# or for hot-reload during dev:
uv run uvicorn iris.app:build_app --factory --reload
```

Open <http://localhost:8000>, click sign in, log in as `alice / alice-pw`,
and you should land on the shell with the Authorization feature visible.

## Seed identities

The `keycloak/realm.json` ships with three users for trying the
capability-adaptive UI. Passwords are intentionally low-entropy because
this stack is local-dev only.

| User    | Password   | Group(s)       | Caps after login                              |
|---------|------------|----------------|-----------------------------------------------|
| alice   | alice-pw   | iris_admins    | global admin (sees everything)                |
| bob     | bob-pw     | db_creators    | can create databases (and admin them)         |
| carol   | carol-pw   | data_team      | regular user — no databases yet (an admin must grant her something) |

`db_creators` and `data_team` are arbitrary group names with no special
meaning to iris; they only matter when an admin grants a tier role to a
group inside the iris UI.

## Common operations

### Add a Keycloak user

The persistent way (survives `docker compose down`):

1. Open <http://localhost:8080/admin> and sign in as the bootstrap admin
   (default `admin / admin`, or whatever you set in `deploy/.env`).
2. Top-left realm switcher → **iris** (NOT `master`).
3. Sidebar → **Users** → **Add user**. Fill in username, email, etc.
4. Click the new user → **Credentials** tab → **Set password** (turn off
   "Temporary" for local dev).
5. **Groups** tab → **Join Group** → pick one of `iris_admins`,
   `db_creators`, `data_team` (or add a new group via sidebar **Groups**
   first).

The user can immediately sign in to iris.

### Add a Keycloak group

Sidebar → **Groups** → **Create group** → enter name → Create.

The group name **must not end in any of iris's reserved CH-role suffixes**:
`_USER`, `_GRP`, `_DBADMIN`, `_DBWRITER`, `_DBREADER`. iris's identifier
validator rejects those at boot, and any user in such a group will fail to
provision in CH.

### Make someone an iris admin

Two channels — pick one (or both):

- **Per-user**: set `CLICKHOUSE_ADMIN_USER=<username>` in the project's
  `.env` and restart iris. On their next login, iris's bootstrap creates
  `<username>_USER` with full CH admin rights and the
  `iris_global_admin` role granted.
- **Per-group**: set `CLICKHOUSE_ADMIN_GROUP=<groupname>` and restart. iris
  creates `<groupname>_GRP` the same way; everyone in the group becomes
  admin on next login.

Both are independently idempotent — re-running with the same value is a
no-op; re-running with a *different* value adds a new admin alongside the
existing ones (the old one stays admin).

To **remove** an admin you set up via these env vars: drop the role in
ClickHouse manually (`DROP ROLE alice_USER` / `DROP ROLE iris_admins_GRP`)
and unset the env var so iris doesn't re-bootstrap it on next start.

### Connect a `clickhouse-client` to the local CH

```bash
./up.sh exec clickhouse clickhouse-client \
  --user iris_admin --password change-me-please
# or from the host:
clickhouse-client --host localhost --port 9000 \
  --user iris_admin --password change-me-please
```

Useful one-liners once connected:

```sql
SHOW USERS;                            -- iris-provisioned users + service user
SHOW ROLES;                            -- including per-database tier roles
SELECT * FROM system.role_grants;      -- who has what
SELECT * FROM system.row_policies;     -- row-policy state
SELECT * FROM system.grants WHERE database = 'marketing';
```

### Reset everything

```bash
./up.sh down                           # stop containers, KEEP data volume
./up.sh down -v                        # stop and remove docker-managed volumes (we use bind mounts so this changes nothing)
rm -rf "${DATA_DIR}/keycloak" "${DATA_DIR}/clickhouse"   # delete persistent state
./up.sh                                # fresh boot, re-imports realm.json
```

Resetting wipes both: Keycloak users you added in the admin console AND
all ClickHouse databases / grants / row policies iris provisioned.

### Stop the stack (keep data)

```bash
./up.sh stop                           # pauses containers
./up.sh start                          # resumes
./up.sh down                           # destroys containers, keeps data
```

### View service logs

```bash
./up.sh logs -f keycloak
./up.sh logs -f clickhouse
```

## Troubleshooting

### Iris "OIDC discovery failed" at startup

- Check Keycloak is healthy: `docker compose ps`.
- Verify the issuer URL responds:
  `curl -s http://localhost:8080/realms/iris/.well-known/openid-configuration | head`.
  If 404, the realm did not import — see `docker compose logs keycloak` for
  the import error, then `docker compose down -v && rm -rf "${DATA_DIR}/keycloak"`
  and retry.
- The OIDC issuer URL must be `http://localhost:...`, NOT
  `http://127.0.0.1:...` or a hostname — the seed realm's `redirectUris`
  list both, but Keycloak compares the issuer claim string-exactly.

### Iris "csrf_mismatch" on login submit

You're probably hitting iris on `127.0.0.1` while the cookie was set for
`localhost` (or vice-versa). Pick one and stick to it across the address
bar, the OIDC redirect URI, and the iris dev server bind.

### "Connection refused" to ClickHouse from iris

- `docker compose ps` — CH should report `healthy`.
- iris connects to `CLICKHOUSE_HOST=localhost`; if you run iris itself
  inside docker, that's wrong (it'd need the service name `clickhouse`).
  This stack assumes iris runs on the host (`uv run iris`).

### Iris bootstrap fails with "GRANT ALL ON \*.\* requires NAMED COLLECTION ADMIN"

The XML overlay in `clickhouse/users.d/iris-overlay.xml` did not apply —
check that the `<iris_admin>` tag matches your `CLICKHOUSE_USER` from
`deploy/.env`. If you changed the username, you must edit the XML tag too
(CH does not expand env vars in XML config).

### Keycloak admin password not what I set

Bootstrap admin env vars are honored ONLY on first boot. To change after,
log in to the admin console and edit the user, OR delete
`${DATA_DIR}/keycloak` and let it re-bootstrap.

### Permission denied on the bind-mounted data dirs

Bind-mounted directories inherit host filesystem permissions. The
ClickHouse entrypoint `chown`s its data dir on startup, so it self-heals.
Keycloak does NOT — if Keycloak fails with "Permission denied" creating
H2 files, the bound `${DATA_DIR}/keycloak` is owned by a user the
in-container Keycloak (uid 1000) cannot write to. Fix once:

```bash
mkdir -p "${DATA_DIR}/keycloak"
chown -R 1000:1000 "${DATA_DIR}/keycloak"
```

Or, if you're on a single-user dev box where you don't care:
`chmod -R 777 "${DATA_DIR}"`.
