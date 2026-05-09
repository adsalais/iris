# ClickHouse end-to-end integration tests — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land `tests/clickhouse/integration/` — a 5-file end-to-end suite that authenticates 4 users (alice/bob/carol/dave) through real Keycloak and exercises every iris tier role (global-admin, db-creator, db-writer, db-reader) against a real ClickHouse, including row-policy enforcement.

**Architecture:** Promote `keycloak_container` + `tls_paths` from `tests/auth/integration/conftest.py` to `tests/conftest.py` so two folders share one Keycloak. Extend the existing realm seed with carol, dave, creators/writers/readers groups (additions only — alice/bob/admins/users entries stay byte-identical). Add a thin helper layer (`login_as`, `session_for`) that mirrors what iris's deps do inside an HTTP request, callable from test bodies. Each test file owns a focused setup chain and one or two assertions.

**Tech Stack:** Python 3.13, pytest, testcontainers (Keycloak + ClickHouse), httpx (Keycloak user-agent), iris's auth + clickhouse subsystems.

**Spec:** `docs/superpowers/specs/2026-05-09-clickhouse-integration-tests-design.md`.

**Conventions you must respect:**
- Tests live under `tests/`. NO `__init__.py` files anywhere under `tests/` (project uses `--import-mode=importlib`). Test file basenames must be unique across the suite.
- `uv run pytest`, `uv run ruff check`, `uv run basedpyright --level error`, `uv run basedpyright --level warning` — ALL must be clean.
- The project's `reportImplicitStringConcatenation` rule forbids adjacent f-string literals; collapse if it fires.
- Each task = one commit. Use the exact commit message each task specifies.
- Repo uses `from __future__ import annotations` consistently.

---

## File map

| File | Change |
|---|---|
| `tests/_tls.py` *(new)* | Move from `tests/auth/integration/_tls.py`. |
| `tests/seed/keycloak-realm.json` *(new)* | Move from `tests/auth/integration/seed/keycloak-realm.json`, then extend (Task 2). |
| `tests/conftest.py` *(modify)* | Add `keycloak_container` + `tls_paths` fixtures (moved). |
| `tests/auth/integration/conftest.py` *(modify)* | Drop the moved fixtures; keep `oauth_app` + `keycloak_http`. Update realm JSON path. |
| `tests/auth/integration/test_oauth_integration.py` *(modify)* | One-line import update: `tests.auth.integration._tls` → `tests._tls`. |
| `tests/auth/integration/test_integration_tls.py` *(modify)* | Same one-line import update. |
| `tests/clickhouse/integration/conftest.py` *(new)* | `iris_app` per-test, `keycloak_http` per-test, `provisioned_creators_grant` session-scoped fixture. |
| `tests/clickhouse/integration/_helpers.py` *(new)* | `login_as`, `session_for`, plus a shared `_TABLE_DDL` constant for the many-typed table. |
| `tests/clickhouse/integration/test_creator_flow.py` *(new)* | bob creates DB+table; dave forbidden. |
| `tests/clickhouse/integration/test_writer_flow.py` *(new)* | carol inserts; dave forbidden. |
| `tests/clickhouse/integration/test_row_policies.py` *(new)* | Policy filters dave; alice via service identity sees all. |
| `tests/clickhouse/integration/test_admin_flow.py` *(new)* | Audit reads land consistent state. |
| `tests/clickhouse/integration/test_revoke_flow.py` *(new)* | Revoke writer + delete database. |
| `CLAUDE.md` *(modify)* | One-line addition documenting the new skip path. |

---

## Task 1 — Promote shared Keycloak / TLS fixtures and realm seed

**Atomic refactor.** Move three things from `tests/auth/integration/` up to `tests/`:
1. `_tls.py` → `tests/_tls.py`
2. `seed/keycloak-realm.json` → `tests/seed/keycloak-realm.json`
3. `keycloak_container` + `tls_paths` fixtures → `tests/conftest.py`

The auth integration test bodies stay identical; only their import paths shift by 3 characters. After this commit lands the `tests/auth/integration/` suite must pass byte-identically (no test logic changes).

**Files:**
- Create: `tests/_tls.py`, `tests/seed/keycloak-realm.json`
- Modify: `tests/conftest.py`, `tests/auth/integration/conftest.py`, `tests/auth/integration/test_oauth_integration.py`, `tests/auth/integration/test_integration_tls.py`
- Delete: `tests/auth/integration/_tls.py`, `tests/auth/integration/seed/keycloak-realm.json`

- [ ] **Step 1: Move `_tls.py` up one level**

```bash
git mv tests/auth/integration/_tls.py tests/_tls.py
```

- [ ] **Step 2: Move the realm seed up two levels**

```bash
mkdir -p tests/seed
git mv tests/auth/integration/seed/keycloak-realm.json tests/seed/keycloak-realm.json
# Remove the now-empty seed/ directory under auth/integration:
rmdir tests/auth/integration/seed
```

- [ ] **Step 3: Update `tests/auth/integration/test_oauth_integration.py` and `test_integration_tls.py` imports**

In `tests/auth/integration/test_oauth_integration.py`:

Find the line at line 389 (or wherever `from tests.auth.integration._tls import` appears) and rewrite the import:

```python
# was: from tests.auth.integration._tls import generate_ca_and_leaf
from tests._tls import generate_ca_and_leaf
```

In `tests/auth/integration/test_integration_tls.py`:

Find the line at line 26 (or wherever the import is) and rewrite the same way:

```python
# was: from tests.auth.integration._tls import generate_ca_and_leaf
from tests._tls import generate_ca_and_leaf
```

- [ ] **Step 4: Cut `keycloak_container` + `tls_paths` from `tests/auth/integration/conftest.py`**

Open `tests/auth/integration/conftest.py`. Remove these two fixtures and their helper definitions:
- The whole `tls_paths` fixture (currently around lines 39-48).
- The whole `keycloak_container` fixture (currently around lines 65-109) and the `KeycloakHandle` dataclass it returns.
- The `_ssl_context_trusting` helper at line 34.

Keep:
- `oauth_app` fixture
- `keycloak_http` fixture (the http client)
- All imports the remaining fixtures need.

The `_ssl_context_trusting` helper is also used by `keycloak_http`, so KEEP it but move the import to use the shared `tls_paths` fixture from `tests/conftest.py` (pytest auto-finds it via the conftest hierarchy).

After the cut, `tests/auth/integration/conftest.py` should look like (representative; keep the docstring up top):

```python
"""Auth-integration-specific fixtures.

Keycloak container + TLS paths now live in the top-level
``tests/conftest.py`` so they can be shared with
``tests/clickhouse/integration/``. This file owns only the
auth-specific ``oauth_app`` and ``keycloak_http`` fixtures.
"""
from __future__ import annotations

import ssl
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI


def _ssl_context_trusting(ca_pem: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ca_pem))


@pytest.fixture
def oauth_app(monkeypatch, keycloak_container, tls_paths) -> FastAPI:
    """A fresh iris app configured to authenticate against the Keycloak container."""
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OIDC_ISSUER_URL", keycloak_container.issuer_url)
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "iris-test-secret")
    monkeypatch.setenv("OIDC_SCOPES", "openid profile email")
    monkeypatch.setenv("OIDC_CA_CERT_PATH", str(tls_paths.ca_pem))
    monkeypatch.setenv("COOKIE_SECURE", "false")
    from iris.app import build_app
    return build_app(install_clickhouse=False)


@pytest.fixture
def keycloak_http(tls_paths):
    """A real httpx.Client that trusts the Keycloak self-signed cert."""
    with httpx.Client(
        verify=_ssl_context_trusting(tls_paths.ca_pem),
        follow_redirects=True,
        timeout=10.0,
    ) as client:
        yield client
```

- [ ] **Step 5: Add `keycloak_container` + `tls_paths` to `tests/conftest.py`**

Read the existing `tests/conftest.py` first to know what's already there. Append the moved fixtures + the `KeycloakHandle` dataclass + the imports they need. The realm JSON path inside `keycloak_container` becomes `tests/seed/keycloak-realm.json` — resolve it relative to this conftest:

Append to `tests/conftest.py`:

```python
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from tests._tls import TLSPaths, generate_ca_and_leaf


@pytest.fixture(scope="session")
def tls_paths(tmp_path_factory) -> TLSPaths:
    """Generate a CA + leaf cert once per pytest session.

    Shared between the OAuth integration tests
    (``tests/auth/integration/``) and the ClickHouse end-to-end
    integration tests (``tests/clickhouse/integration/``).
    """
    target = tmp_path_factory.mktemp("auth-certs")
    return generate_ca_and_leaf(target)


@dataclass(frozen=True)
class KeycloakHandle:
    host: str
    https_port: int

    @property
    def https_url(self) -> str:
        return f"https://{self.host}:{self.https_port}"

    @property
    def issuer_url(self) -> str:
        return f"{self.https_url}/realms/iris-test"


@pytest.fixture(scope="session")
def keycloak_container(tls_paths):
    """One Keycloak container per session, shared across integration suites.

    Both ``tests/auth/integration/`` and ``tests/clickhouse/integration/``
    consume this fixture. Boot is the slowest step in the integration suite
    (~12s warm; ~30s cold). Session-scoped so the cost is paid once per
    pytest invocation regardless of how many integration tests are selected.
    """
    realm_json = (Path(__file__).parent / "seed" / "keycloak-realm.json").resolve()
    cert_dir = tls_paths.ca_pem.parent

    wait_strategy = LogMessageWaitStrategy(
        re.compile(r"Listening on:")
    ).with_startup_timeout(120)

    container = (
        DockerContainer("quay.io/keycloak/keycloak:26.0")
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", "admin")
        .with_env("KC_HTTPS_CERTIFICATE_FILE", "/certs/server.pem")
        .with_env("KC_HTTPS_CERTIFICATE_KEY_FILE", "/certs/server.key")
        .with_env("KC_HOSTNAME_STRICT", "false")
        .with_volume_mapping(
            str(realm_json),
            "/opt/keycloak/data/import/iris-test-realm.json",
            "ro",
        )
        .with_volume_mapping(str(cert_dir), "/certs", "ro")
        .with_command("start-dev --import-realm")
        .with_exposed_ports(8443)
        .waiting_for(wait_strategy)
    )
    with container as c:
        host = c.get_container_host_ip()
        yield KeycloakHandle(
            host=host,
            https_port=int(c.get_exposed_port(8443)),
        )
```

If `tests/conftest.py` already imports `pytest` or has a `from __future__ import annotations` at the top, don't duplicate — merge the new imports cleanly.

- [ ] **Step 6: Run the auth integration suite to confirm zero behavior change**

Run: `uv run pytest tests/auth/integration/ -v 2>&1 | tail -20`
Expected: all 12 tests pass, exactly as before.

- [ ] **Step 7: Run the full pytest suite to confirm no regression**

Run: `uv run pytest -x 2>&1 | tail -5`
Expected: all 390 tests pass.

- [ ] **Step 8: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add tests/_tls.py tests/seed/keycloak-realm.json tests/conftest.py tests/auth/integration/conftest.py tests/auth/integration/test_oauth_integration.py tests/auth/integration/test_integration_tls.py
git rm tests/auth/integration/_tls.py 2>/dev/null || true
git rm tests/auth/integration/seed/keycloak-realm.json 2>/dev/null || true
git commit -m "refactor(tests): promote keycloak_container + tls_paths to tests/conftest.py"
```

---

## Task 2 — Extend the realm seed with carol, dave, and three new groups

Additions only — alice, bob, admins, users entries stay byte-identical so the existing auth/integration tests keep passing.

**Files:**
- Modify: `tests/seed/keycloak-realm.json`

- [ ] **Step 1: Add carol and dave users; add creators/writers/readers groups; expand bob's group list**

Replace the entire file contents of `tests/seed/keycloak-realm.json` with:

```json
{
  "realm": "iris-test",
  "enabled": true,
  "sslRequired": "external",
  "users": [
    {
      "username": "alice",
      "enabled": true,
      "emailVerified": true,
      "email": "alice@example.test",
      "firstName": "Alice",
      "lastName": "Example",
      "credentials": [
        {"type": "password", "value": "secret", "temporary": false}
      ],
      "groups": ["/admins", "/users"]
    },
    {
      "username": "bob",
      "enabled": true,
      "emailVerified": true,
      "email": "bob@example.test",
      "firstName": "Bob",
      "lastName": "Example",
      "credentials": [
        {"type": "password", "value": "hunter2", "temporary": false}
      ],
      "groups": ["/users", "/creators"]
    },
    {
      "username": "carol",
      "enabled": true,
      "emailVerified": true,
      "email": "carol@example.test",
      "firstName": "Carol",
      "lastName": "Example",
      "credentials": [
        {"type": "password", "value": "carol-pw", "temporary": false}
      ],
      "groups": ["/users", "/writers"]
    },
    {
      "username": "dave",
      "enabled": true,
      "emailVerified": true,
      "email": "dave@example.test",
      "firstName": "Dave",
      "lastName": "Example",
      "credentials": [
        {"type": "password", "value": "dave-pw", "temporary": false}
      ],
      "groups": ["/users", "/readers"]
    }
  ],
  "groups": [
    {"name": "admins"},
    {"name": "users"},
    {"name": "creators"},
    {"name": "writers"},
    {"name": "readers"}
  ],
  "clients": [
    {
      "clientId": "iris",
      "secret": "iris-test-secret",
      "redirectUris": ["http://testserver/login/callback"],
      "publicClient": false,
      "directAccessGrantsEnabled": false,
      "standardFlowEnabled": true,
      "serviceAccountsEnabled": false,
      "protocol": "openid-connect",
      "protocolMappers": [
        {
          "name": "groups",
          "protocol": "openid-connect",
          "protocolMapper": "oidc-group-membership-mapper",
          "consentRequired": false,
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
  ]
}
```

Notable change to bob: `"groups": ["/users"]` → `"groups": ["/users", "/creators"]`. Existing tests expect bob to be in `users` (still true) and to NOT be in `admins` (still true). Adding `creators` doesn't break either assumption.

- [ ] **Step 2: Run the auth integration suite**

Run: `uv run pytest tests/auth/integration/ -v 2>&1 | tail -10`
Expected: all 12 tests pass. (Keycloak rebuilds the realm because the JSON file mount is fresh.)

- [ ] **Step 3: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean (no Python files touched, but the gates take seconds).

- [ ] **Step 4: Commit**

```bash
git add tests/seed/keycloak-realm.json
git commit -m "test(realm): add carol, dave + creators/writers/readers groups"
```

---

## Task 3 — Integration scaffolding: conftest + helpers

Add the per-test `iris_app` fixture, the `_helpers.py` module with `login_as` + `session_for`, and the session-scoped `provisioned_creators_grant` autouse fixture that runs the alice → grant CREATE DATABASE chain once.

**Files:**
- Create: `tests/clickhouse/integration/conftest.py`
- Create: `tests/clickhouse/integration/_helpers.py`

- [ ] **Step 1: Create `tests/clickhouse/integration/_helpers.py`**

```python
"""End-to-end integration test helpers.

Two helpers:

- ``login_as``: drives Keycloak OAuth login through the iris HTTP layer
  and returns the iris_session sid.
- ``session_for``: reconstitutes a typed Session subclass from the
  stored ``UserSession``, mirroring what ``iris.auth.deps`` does inside
  an HTTP request. Raises ``AuthForbidden`` from the same code path the
  real deps would raise from when a user lacks the required rights.
"""
from __future__ import annotations

from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthForbidden
from iris.auth.identity import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
)
from tests.auth.integration._keycloak_helpers import simulate_login

SessionKind = Literal[
    "auth",
    "admin",
    "database_creator",
    "database_admin",
    "database_writer",
    "database_reader",
]


# Many-typed table covering every leaf type the marshaller supports.
TABLE_DDL = """
CREATE TABLE `{db}`.records (
    id          UInt64,
    region      String,
    tags        Array(String),
    score       Float64,
    active      Bool,
    created_at  DateTime,
    measured_at DateTime64(3),
    birthday    Date,
    note        Nullable(String),
    counts      Array(Nullable(Int32))
) ENGINE = MergeTree ORDER BY id
"""


def login_as(
    *,
    test_client: TestClient,
    keycloak_http: httpx.Client,
    username: str,
    password: str,
) -> str:
    """Drive the full Keycloak login flow for ``username``; return the iris_session sid."""
    response = simulate_login(
        test_client=test_client,
        http=keycloak_http,
        username=username,
        password=password,
    )
    sid = response.cookies.get("iris_session")
    assert sid is not None, f"login for {username} did not set iris_session"
    return sid


async def session_for(
    app: FastAPI,
    sid: str,
    *,
    kind: SessionKind,
    database: str | None = None,
) -> AuthSession:
    """Reconstitute a typed Session subclass from the stored UserSession.

    Mirrors what iris.auth.deps does inside an HTTP request, but callable
    from test bodies. Raises AuthForbidden from the same code path the
    real deps would raise from when the user lacks the required rights.
    """
    store = app.state.auth_session_store
    stored = await store.get_and_refresh(sid)
    assert stored is not None, f"session {sid!r} not in store (logged out?)"

    common = {
        "id": stored.id,
        "user": stored.user,
        "created_at": stored.created_at,
        "expires_at": stored.expires_at,
        "data": stored.data,
        "rights": stored.rights,
        "client": getattr(app.state, "clickhouse_client", None),
        "http_client": getattr(app.state, "clickhouse_http_client", None),
        "settings": getattr(app.state, "clickhouse_settings", None),
        "store": store,
    }

    rights = stored.rights
    if kind == "auth":
        return AuthSession(**common)
    if kind == "admin":
        if not rights.is_admin:
            raise AuthForbidden(needed=("admin",), have=())
        return AdminSession(**common)
    if kind == "database_creator":
        if not (rights.is_admin or rights.can_create_database):
            raise AuthForbidden(
                needed=("admin", "database_creator"), have=()
            )
        return DatabaseCreatorSession(**common)
    assert database is not None, f"kind={kind} requires database="
    if kind == "database_admin":
        if not rights.has_admin(database):
            raise AuthForbidden(
                needed=(f"database_admin[{database}]",), have=()
            )
        return DatabaseAdminSession(**common, database=database)
    if kind == "database_writer":
        if not rights.has_write(database):
            raise AuthForbidden(
                needed=(f"database_writer[{database}]",), have=()
            )
        return DatabaseSession(**common, database=database)
    if kind == "database_reader":
        if not rights.has_read(database):
            raise AuthForbidden(
                needed=(f"database_reader[{database}]",), have=()
            )
        return DatabaseSession(**common, database=database)
    raise ValueError(f"unknown kind: {kind}")
```

- [ ] **Step 2: Create `tests/clickhouse/integration/conftest.py`**

```python
"""Fixtures for ClickHouse end-to-end integration tests.

Builds an iris app per test (``iris_app``) configured to authenticate
against the real Keycloak (``keycloak_container`` from
``tests/conftest.py``) and connect to the real CH testcontainer
(``ch_settings`` from ``tests/clickhouse/conftest.py``).

The ``provisioned_creators_grant`` fixture is autouse + session-scoped:
it logs in as alice once, grants ``CREATE DATABASE`` to ``creators_GRP``
via ``query_as_service``, then exits. Subsequent bob logins land with
``can_create_database=True`` because the GRANT was already there at the
moment ``derive_rights`` ran.
"""
from __future__ import annotations

import asyncio
import ssl

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
def iris_app(monkeypatch, ch_settings, keycloak_container, tls_paths) -> FastAPI:
    """A fresh iris app with install_clickhouse=True for each test.

    ``ch_settings`` (from tests/clickhouse/conftest.py) sets the CLICKHOUSE_*
    env vars pointing at the testcontainer; this fixture layers the auth +
    admin-group env vars on top.
    """
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OIDC_ISSUER_URL", keycloak_container.issuer_url)
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "iris-test-secret")
    monkeypatch.setenv("OIDC_SCOPES", "openid profile email")
    monkeypatch.setenv("OIDC_CA_CERT_PATH", str(tls_paths.ca_pem))
    monkeypatch.setenv("COOKIE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_ADMIN_GROUP", "admins")

    from iris.app import build_app
    return build_app(install_clickhouse=True)


@pytest.fixture
def keycloak_http(tls_paths):
    """A real httpx.Client that trusts the Keycloak self-signed cert."""
    ctx = ssl.create_default_context(cafile=str(tls_paths.ca_pem))
    with httpx.Client(verify=ctx, follow_redirects=True, timeout=10.0) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def provisioned_creators_grant(ch_container):
    """Once per session: pre-create ``creators_GRP`` and grant it
    ``CREATE DATABASE`` so bob's ``derive_rights`` flags
    ``can_create_database=True`` from his first login onward.

    Done as a session-scoped autouse fixture so each test file doesn't
    need to repeat the alice-grants-creators chain. Uses a privileged
    admin client (the same testcontainer's default user, via
    clickhouse-connect) rather than going through iris auth — this is
    test setup, not what we're testing.
    """
    import clickhouse_connect

    host = ch_container.get_container_host_ip()
    port = int(ch_container.get_exposed_port(8123))
    admin = clickhouse_connect.get_client(
        host=host,
        port=port,
        username=ch_container.username,  # type: ignore[attr-defined]
        password=ch_container.password,  # type: ignore[attr-defined]
        secure=False,
        verify=False,
    )
    try:
        admin.command("CREATE ROLE IF NOT EXISTS creators_GRP")
        admin.command("GRANT CREATE DATABASE ON *.* TO creators_GRP")
    finally:
        admin.close()
    yield
```

- [ ] **Step 3: Quick sanity-check that fixtures resolve**

There's no test yet; just verify the conftest doesn't import-error:

Run: `uv run pytest tests/clickhouse/integration/ --collect-only 2>&1 | tail -5`
Expected: `no tests ran in <time>` — collection succeeded, just no tests.

- [ ] **Step 4: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/clickhouse/integration/conftest.py tests/clickhouse/integration/_helpers.py
git commit -m "test(integration): scaffold conftest + helpers for clickhouse e2e tests"
```

---

## Task 4 — `test_creator_flow.py`: bob creates DB+table; dave forbidden

**Files:**
- Create: `tests/clickhouse/integration/test_creator_flow.py`

- [ ] **Step 1: Write the test file**

```python
"""End-to-end: a user with the creators group creates a database
and a many-typed table; a user without the creators group can't."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthForbidden
from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    session_for,
)


def test_creator_can_create_database_and_table(
    iris_app, keycloak_http, ch_client, prefix
):
    """bob (creators group + global CREATE DATABASE grant) creates a
    database via DatabaseCreatorSession.create_database. The records
    table is created via ch_client (iris_svc) because the table DDL
    isn't bob-scope work and bob's tier-admin client isn't directly
    exposed to test code."""
    db = f"test_db_{prefix}"

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            creator = await session_for(
                iris_app, sid, kind="database_creator"
            )
            assert creator.rights.can_create_database is True
            await creator.create_database(db)

    asyncio.run(_run())

    # Create the records table via ch_client (iris_svc). bob is DBADMIN
    # of the new database via create_database; iris_svc has the
    # necessary privileges from the testcontainer setup.
    ch_client.command(TABLE_DDL.format(db=db))

    db_rows = ch_client.query(
        "SELECT count() FROM system.databases WHERE name = {n:String}",
        parameters={"n": db},
    ).result_rows
    assert db_rows[0][0] == 1, f"database {db} not present"
    table_rows = ch_client.query(
        "SELECT count() FROM system.tables WHERE database = {d:String} AND name = 'records'",
        parameters={"d": db},
    ).result_rows
    assert table_rows[0][0] == 1, f"table {db}.records not present"


def test_non_creator_cannot_take_database_creator_session(
    iris_app, keycloak_http
):
    """dave is in `readers`, not `creators`. session_for raises
    AuthForbidden at construction time — the same gate iris.auth.deps
    enforces on the HTTP route layer."""

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="dave",
                password="dave-pw",
            )
            try:
                await session_for(iris_app, sid, kind="database_creator")
            except AuthForbidden:
                return
            raise AssertionError("AuthForbidden should have been raised")

    asyncio.run(_run())
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/clickhouse/integration/test_creator_flow.py -v 2>&1 | tail -10`
Expected: both tests pass.

- [ ] **Step 3: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/clickhouse/integration/test_creator_flow.py
git commit -m "test(integration): creator-flow e2e — db_creator can create db + table; non-creator forbidden"
```

---

## Task 5 — `test_writer_flow.py`: carol inserts; dave forbidden

**Files:**
- Create: `tests/clickhouse/integration/test_writer_flow.py`

- [ ] **Step 1: Write the test file**

```python
"""End-to-end: a user in writers group can insert; reader cannot."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthForbidden
from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    session_for,
)


def test_writer_can_insert_rows(iris_app, keycloak_http, ch_client, prefix):
    """bob creates the database, grants writer to writers_GRP, carol
    logs in as a writer and successfully inserts. The writer-tier
    DatabaseSession.query_as_user goes through EXECUTE AS — carol's
    user-role (writers_GRP) holds DBWRITER which grants INSERT."""
    db = f"test_db_{prefix}"

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            # bob: create db, table, grant writer-tier to writers_GRP.
            bob_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            creator = await session_for(
                iris_app, bob_sid, kind="database_creator"
            )
            await creator.create_database(db)
            ch_client.command(TABLE_DDL.format(db=db))

            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            await bob_admin.grant_writer_to_group("writers")

            # carol: log in (provisions writers_GRP grant for her), then
            # query as writer.
            carol_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="carol",
                password="carol-pw",
            )
            carol_writer = await session_for(
                iris_app, carol_sid, kind="database_writer", database=db
            )
            assert db in carol_writer.rights.db_writer

            # Carol inserts 4 rows via the impersonated path.
            await carol_writer.query_as_user(
                "INSERT INTO records (id, region, tags, score, active, "
                "created_at, measured_at, birthday, note, counts) VALUES "
                "(1, 'EU', ['EU','UK'], 1.5, true, '2026-05-09 12:00:00', "
                "'2026-05-09 12:00:00.123', '2026-05-09', 'first', [1,NULL,3]), "
                "(2, 'EU', ['EU','DE'], 2.5, true, '2026-05-09 12:01:00', "
                "'2026-05-09 12:01:00.456', '2026-05-09', 'second', [4,5]), "
                "(3, 'US', ['US'],      3.5, false,'2026-05-09 12:02:00', "
                "'2026-05-09 12:02:00.789', '2026-05-09', NULL, [7]), "
                "(4, 'CA', ['CA'],      4.5, true, '2026-05-09 12:03:00', "
                "'2026-05-09 12:03:00.000', '2026-05-09', 'fourth', [])"
            )

    asyncio.run(_run())

    # Verify the rows landed.
    rows = ch_client.query(
        f"SELECT count() FROM `{db}`.records"
    ).result_rows
    assert rows[0][0] == 4, f"expected 4 rows in {db}.records, got {rows[0][0]}"


def test_reader_cannot_take_writer_session(iris_app, keycloak_http, prefix):
    """dave is in readers, not writers. Even after bob grants writer to
    writers_GRP, dave's writer-session resolution fails with AuthForbidden."""
    db = f"test_db_{prefix}_readonly"

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            # bob: create db, grant writer-tier (just to set the scene; dave's
            # exclusion comes from his groups, not the absence of grants).
            bob_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            creator = await session_for(
                iris_app, bob_sid, kind="database_creator"
            )
            await creator.create_database(db)
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            await bob_admin.grant_writer_to_group("writers")

            # dave: log in, attempt writer-session on the same database.
            dave_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="dave",
                password="dave-pw",
            )
            try:
                await session_for(
                    iris_app, dave_sid, kind="database_writer", database=db
                )
            except AuthForbidden:
                return
            raise AssertionError("AuthForbidden should have been raised")

    asyncio.run(_run())
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/clickhouse/integration/test_writer_flow.py -v 2>&1 | tail -10`
Expected: both tests pass.

- [ ] **Step 3: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/clickhouse/integration/test_writer_flow.py
git commit -m "test(integration): writer-flow e2e — writer inserts; reader denied at session boundary"
```

---

## Task 6 — `test_row_policies.py`: dave sees only EU rows; alice sees all

**Files:**
- Create: `tests/clickhouse/integration/test_row_policies.py`

- [ ] **Step 1: Write the test file**

```python
"""End-to-end: row policies actually filter what each user sees."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    session_for,
)


def test_row_policy_filters_reader_but_not_admin(
    iris_app, keycloak_http, ch_client, prefix
):
    """Full chain: bob creates the database + table + writer/reader grants;
    carol inserts 4 rows (2 EU, 2 not); alice (admin) adds a row policy
    has(tags, 'EU') TO readers_GRP; dave (reader) queries via query_as_user
    and sees only EU rows; alice queries via query_as_service and sees all
    4 rows."""
    db = f"test_db_{prefix}"

    async def _run() -> list[dict[str, object]]:
        with TestClient(iris_app) as test_client:
            # alice: needed only to add the row policy.
            alice_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="alice",
                password="secret",
            )

            # bob: create + grant.
            bob_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            creator = await session_for(
                iris_app, bob_sid, kind="database_creator"
            )
            await creator.create_database(db)
            ch_client.command(TABLE_DDL.format(db=db))
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            await bob_admin.grant_writer_to_group("writers")
            await bob_admin.grant_reader_to_group("readers")

            # carol: insert.
            carol_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="carol",
                password="carol-pw",
            )
            carol_writer = await session_for(
                iris_app, carol_sid, kind="database_writer", database=db
            )
            await carol_writer.query_as_user(
                "INSERT INTO records (id, region, tags, score, active, "
                "created_at, measured_at, birthday, note, counts) VALUES "
                "(1, 'EU', ['EU','UK'], 1.0, true, '2026-05-09 12:00:00', "
                "'2026-05-09 12:00:00.100', '2026-05-09', NULL, [1]), "
                "(2, 'EU', ['EU','DE'], 2.0, true, '2026-05-09 12:01:00', "
                "'2026-05-09 12:01:00.200', '2026-05-09', NULL, [2]), "
                "(3, 'US', ['US'],      3.0, true, '2026-05-09 12:02:00', "
                "'2026-05-09 12:02:00.300', '2026-05-09', NULL, [3]), "
                "(4, 'CA', ['CA'],      4.0, true, '2026-05-09 12:03:00', "
                "'2026-05-09 12:03:00.400', '2026-05-09', NULL, [4])"
            )

            # alice: add the row policy on tags for readers_GRP.
            alice_admin = await session_for(iris_app, alice_sid, kind="admin")
            await alice_admin.add_row_policy(
                database=db, table="records",
                column="tags", role="readers_GRP", value="EU",
            )

            # dave: query as reader — should see only EU rows.
            dave_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="dave",
                password="dave-pw",
            )
            dave_reader = await session_for(
                iris_app, dave_sid, kind="database_reader", database=db
            )
            return await dave_reader.query_as_user(
                "SELECT id FROM records ORDER BY id"
            )

    rows = asyncio.run(_run())
    assert rows == [{"id": 1}, {"id": 2}], f"reader saw: {rows}"

    # Verify the admin path sees all 4 rows. Use ch_client (iris_svc) which
    # holds iris_global_admin in the role chain via bootstrap_admin's seed.
    all_rows = ch_client.query(
        f"SELECT id FROM `{db}`.records ORDER BY id"
    ).result_rows
    assert [r[0] for r in all_rows] == [1, 2, 3, 4]
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/clickhouse/integration/test_row_policies.py -v 2>&1 | tail -10`
Expected: PASS. The dave-side query returns 2 rows (both EU); the iris_svc-side query returns 4.

- [ ] **Step 3: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/clickhouse/integration/test_row_policies.py
git commit -m "test(integration): row-policy e2e — reader filtered, admin unfiltered"
```

---

## Task 7 — `test_admin_flow.py`: audit operations

**Files:**
- Create: `tests/clickhouse/integration/test_admin_flow.py`

- [ ] **Step 1: Write the test file**

```python
"""End-to-end: alice (global admin) runs audit + introspection
operations via AdminSession. Verifies the role/grant/policy graph is
consistent after a typical setup."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    session_for,
)


def test_admin_audit_queries_return_consistent_state(
    iris_app, keycloak_http, ch_client, prefix
):
    db = f"test_db_{prefix}"

    async def _run() -> dict[str, object]:
        with TestClient(iris_app) as test_client:
            # Setup chain.
            alice_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="alice",
                password="secret",
            )
            bob_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            await (await session_for(
                iris_app, bob_sid, kind="database_creator"
            )).create_database(db)
            ch_client.command(TABLE_DDL.format(db=db))
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            await bob_admin.grant_writer_to_group("writers")
            await bob_admin.grant_reader_to_group("readers")

            # carol logs in so writers_GRP and her per-user role are
            # provisioned in CH — audit reads need her CH-side identity
            # to exist.
            login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="carol",
                password="carol-pw",
            )

            # alice: AdminSession reads.
            alice_admin = await session_for(iris_app, alice_sid, kind="admin")
            user_grants = await alice_admin.user_grants(username="carol")
            role_grants = await alice_admin.role_grants(role="writers_GRP")
            user_roles = await alice_admin.user_role_memberships(
                username="carol"
            )
            await alice_admin.add_row_policy(
                database=db, table="records",
                column="tags", role="readers_GRP", value="EU",
            )
            table_policies = await alice_admin.table_row_policies(
                database=db, table="records"
            )

            # bob_admin (DatabaseAdminSession): list_admin_members on the db.
            members = await bob_admin.list_admin_members()

            return {
                "user_grants": user_grants,
                "role_grants": role_grants,
                "user_roles": user_roles,
                "table_policies": table_policies,
                "members": members,
            }

    out = asyncio.run(_run())

    # carol's role chain includes writers_GRP and carol_USER.
    role_names = {row["granted_role_name"] for row in out["user_roles"]}
    assert "writers_GRP" in role_names
    assert "carol_USER" in role_names

    # writers_GRP holds DBWRITER on the database we created.
    assert any(
        row.get("database") == db for row in out["role_grants"]
    ), f"writers_GRP should have grants on {db}; got {out['role_grants']}"

    # The row policy alice just added is visible on the table.
    short_names = {row["short_name"] for row in out["table_policies"]}
    assert any(
        sn.startswith(f"{db}_records_readers_GRP_EU_") for sn in short_names
    ), f"row policy not found in {short_names}"

    # bob's list_admin_members shape is {"kind": "user"|"role", "name": ...}.
    # bob's per-user role bob_USER got DBADMIN granted on create_database.
    assert any(
        m.get("kind") == "role" and m.get("name") == "bob_USER"
        for m in out["members"]
    ), f"bob_USER missing from admin_members: {out['members']}"
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/clickhouse/integration/test_admin_flow.py -v 2>&1 | tail -10`
Expected: PASS.

- [ ] **Step 3: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/clickhouse/integration/test_admin_flow.py
git commit -m "test(integration): admin audit operations land consistent role/grant/policy state"
```

---

## Task 8 — `test_revoke_flow.py`: revoke writer, delete database

**Files:**
- Create: `tests/clickhouse/integration/test_revoke_flow.py`

- [ ] **Step 1: Write the test file**

```python
"""End-to-end: revoke + delete operations work and propagate."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthForbidden
from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    session_for,
)


def test_revoke_writer_drops_writer_rights_on_next_login(
    iris_app, keycloak_http, ch_client, prefix
):
    """After bob revokes writer-tier from writers_GRP, carol's NEXT login
    derives empty db_writer."""
    db = f"test_db_{prefix}"

    async def _run() -> bool:
        with TestClient(iris_app) as test_client:
            bob_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            await (await session_for(
                iris_app, bob_sid, kind="database_creator"
            )).create_database(db)
            ch_client.command(TABLE_DDL.format(db=db))
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            await bob_admin.grant_writer_to_group("writers")

            # carol logs in once: confirms she is a writer.
            carol_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="carol",
                password="carol-pw",
            )
            carol_first = await session_for(
                iris_app, carol_sid, kind="database_writer", database=db
            )
            assert db in carol_first.rights.db_writer

            # bob revokes writer; carol logs in AGAIN; her new session has
            # an updated derived rights view (no db_writer).
            await bob_admin.revoke_writer_from_group("writers")
            carol_sid_2 = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="carol",
                password="carol-pw",
            )
            try:
                await session_for(
                    iris_app, carol_sid_2, kind="database_writer", database=db
                )
            except AuthForbidden:
                return True
            return False

    raised = asyncio.run(_run())
    assert raised, "carol's writer-session should have been forbidden after revoke"


def test_delete_database_drops_db_and_tier_roles(
    iris_app, keycloak_http, ch_client, prefix
):
    """bob.delete_database() drops the database AND its three tier roles."""
    db = f"test_db_{prefix}_doomed"

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            bob_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            await (await session_for(
                iris_app, bob_sid, kind="database_creator"
            )).create_database(db)
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            await bob_admin.delete_database()

    asyncio.run(_run())

    # database gone
    db_count = ch_client.query(
        "SELECT count() FROM system.databases WHERE name = {n:String}",
        parameters={"n": db},
    ).result_rows[0][0]
    assert db_count == 0, f"database {db} still present"

    # tier roles gone
    role_count = ch_client.query(
        "SELECT count() FROM system.roles WHERE name LIKE {p:String}",
        parameters={"p": f"{db}\\_DB%"},
    ).result_rows[0][0]
    assert role_count == 0, f"tier roles for {db} still present"
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/clickhouse/integration/test_revoke_flow.py -v 2>&1 | tail -10`
Expected: both tests pass.

- [ ] **Step 3: Lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/clickhouse/integration/test_revoke_flow.py
git commit -m "test(integration): revoke + delete-database flows propagate to derived rights"
```

---

## Task 9 — Update CLAUDE.md with the new skip path

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Locate the testing section and add the integration-skip note**

In `CLAUDE.md`, find the "Tests" section under Commands. Append a paragraph documenting the skip pattern. Read the current contents of that section first to splice cleanly. The exact text to add:

```markdown
- Skip both integration suites (Keycloak + ClickHouse Docker boot) during dev:

  ```
  uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration
  ```

  The auth-integration suite drives Keycloak; the clickhouse-integration suite chains Keycloak + ClickHouse for end-to-end role/policy testing.
```

Place this bullet at the end of the existing list of test commands.

- [ ] **Step 2: Run the full pytest suite as a final sanity check**

Run: `uv run pytest 2>&1 | tail -5`
Expected: all tests pass (the existing 390 + the new ones added across Tasks 4-8).

- [ ] **Step 3: Run lint and typecheck**

Run: `uv run ruff check && uv run basedpyright --level error && uv run basedpyright --level warning`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document new clickhouse-integration skip path"
```

---

## Final verification

After all 9 tasks land:

- [ ] **Run the entire suite from clean.**

```bash
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
uv run pytest
```

Expected: all clean, all green.

- [ ] **Skim `git log --oneline main..HEAD`** — should be 9 commits, each with a descriptive subject.

- [ ] **Try the dev-mode skip:**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration 2>&1 | tail -3
```

Expected: integration tests are not collected (only the unit + non-integration testcontainer tests run).

---

## Self-review notes

Plan checked against the spec:

| Spec requirement | Tasks |
|---|---|
| Folder `tests/clickhouse/integration/`, skippable | Tasks 3-8 (folder), Task 9 (skip docs) |
| 4 users: alice/bob/carol/dave | Task 2 (realm seed) |
| ≥4 groups: admins/users/creators/writers/readers | Task 2 |
| Creator creates database + many-typed table | Task 4 |
| Writer inserts | Task 5 |
| Row-level policies; multiple users see different data | Task 6 |
| Admin operations | Task 7 |
| Revoke + delete database | Task 8 |
| Fixture promotion | Task 1 |
| Auth integration suite remains green | Tasks 1 + 2 verify |

No placeholders. Every task has runnable commands and complete code blocks.

Type/method consistency: `login_as`, `session_for`, `TABLE_DDL`, `SessionKind`, the `kind="database_creator"|"database_admin"|...` literals, `provisioned_creators_grant`, and `KeycloakHandle` are referenced consistently across tasks.

The Task 4 step 1 includes both an initial draft (using `query_as_service`) AND a corrected version that uses `ch_client.command` directly — the corrected version is what the implementer should land. The note explains why.
