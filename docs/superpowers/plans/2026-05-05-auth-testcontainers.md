# Auth Integration Tests with Testcontainers — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an integration tier under `tests/auth/integration/` that exercises the LDAP and OAuth providers (provider + route layers, happy + failure paths, full TLS) against real `bitnami/openldap` and `quay.io/keycloak/keycloak` containers managed by `testcontainers-python`. Existing offline tests stay as the fast unit-level tier.

**Architecture:** Session-scoped Docker containers (mirroring `tests/clickhouse/conftest.py`), seeded declaratively (LDIF + Keycloak realm-import JSON), with a self-signed CA generated once per session via `cryptography`. Each test gets a fresh `app` via `monkeypatch.setenv` overrides + `build_app()`. A small helper drives Keycloak's authorize → login form → callback dance through `TestClient` for OAuth route-level tests.

**Tech Stack:** `testcontainers-python` (`testcontainers[keycloak]`), `bitnami/openldap:2.6`, `quay.io/keycloak/keycloak:26.0`, `cryptography` (transitive), `ldap3`, `httpx`, `fastapi.testclient`, `pytest`, `pytest-monkeypatch`.

**Spec:** `docs/superpowers/specs/2026-05-05-auth-testcontainers-design.md`

---

## File Structure

**Created:**

| File | Responsibility |
|------|----------------|
| `tests/auth/integration/conftest.py` | Session-scoped `openldap_container` + `keycloak_container` + `tls_paths` fixtures; per-test `ldap_app` / `oauth_app` fixtures |
| `tests/auth/integration/_tls.py` | Generate CA + leaf cert (pure `cryptography`, no openssl shell-out) |
| `tests/auth/integration/_keycloak_helpers.py` | `simulate_login(...)` drives the Keycloak login form |
| `tests/auth/integration/seed/ldap.ldif` | Declarative directory: alice/bob/carol + admins/users groups |
| `tests/auth/integration/seed/keycloak-realm.json` | Declarative realm: alice/bob, groups, iris client + groups mapper |
| `tests/auth/integration/test_ldap_integration.py` | LDAP provider + route tests (~9) |
| `tests/auth/integration/test_oauth_integration.py` | OAuth provider + route tests (~8) |

**Modified:**

| File | Change |
|------|--------|
| `pyproject.toml` | Add `testcontainers[keycloak]` to dev deps |
| `src/iris/auth/config.py` | Add `OIDCSettings.ca_cert_path: str \| None`; read `OIDC_CA_CERT_PATH` in `from_env()` |
| `src/iris/auth/providers/oauth.py` | When `settings.ca_cert_path` is set and `_http_transport` is None, build httpx clients with `verify=settings.ca_cert_path` |
| `tests/auth/test_provider_oauth.py` | Add a regression test asserting the new `ca_cert_path` field defaults to `None` and existing offline tests still pass |
| `CLAUDE.md` | Document the integration tier, the `--ignore=tests/auth/integration` opt-out, and the runtime cost |

**Untouched (intentional):**
- `tests/auth/test_provider_ldap.py` and `tests/auth/test_provider_oauth.py` — existing offline tests stay as the fast tier.
- `src/iris/auth/providers/ldap.py` — already accepts `LDAP_CA_CERT_PATH`; no production code change needed.
- `tests/conftest.py` — the integration suite uses its own `monkeypatch.setenv` to override `AUTH_METHOD`; no changes here.

`tests/auth/integration/` does NOT get an `__init__.py` (per `--import-mode=importlib`). Helper modules use `_`-prefixes so pytest doesn't collect them. File basenames (`test_ldap_integration.py`, `test_oauth_integration.py`) are unique across the suite.

---

## Task 1: Add `testcontainers[keycloak]` dev dep and verify import

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add the dev dep**

```bash
uv add --dev "testcontainers[keycloak]"
```

- [ ] **Step 2: Verify the Keycloak module is importable**

```bash
uv run python -c "from testcontainers.keycloak import KeycloakContainer; print('OK', KeycloakContainer.__name__)"
```

Expected: `OK KeycloakContainer`

If the import fails (the `keycloak` extra isn't published in the pinned version of `testcontainers`), STOP and switch to the fallback — install plain `testcontainers`, then write a generic-DockerContainer wrapper as part of Task 6 (Keycloak fixture). The risk is captured in the spec; the fallback path is ~30 lines.

- [ ] **Step 3: Verify the testsuite still passes**

```bash
uv run pytest -q
```

Expected: all existing tests pass; new dev dep is silently available.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore(deps): add testcontainers[keycloak] dev dep for auth integration tests"
```

---

## Task 2: Scaffold `tests/auth/integration/` directory

**Files:**
- Create: `tests/auth/integration/conftest.py` (empty placeholder, fixtures added in later tasks)
- Create: `tests/auth/integration/seed/.gitkeep` (empty file, pin the directory in git)
- Create: `tests/auth/integration/test_ldap_integration.py` (placeholder with one trivial test to verify collection)
- Create: `tests/auth/integration/test_oauth_integration.py` (placeholder with one trivial test to verify collection)

- [ ] **Step 1: Create the placeholder files**

`tests/auth/integration/conftest.py`:
```python
"""Fixtures for the auth integration tier (LDAP + OAuth via real containers).

Spins up bitnami/openldap and Keycloak via testcontainers-python, generates a
self-signed CA + leaf cert in pure Python, and yields per-test FastAPI apps
configured to use the real provider.

This conftest layers on top of tests/conftest.py: the parent conftest sets
AUTH_METHOD=mock at module scope; integration tests use monkeypatch.setenv to
override that for the duration of the test.

Run only this tier:        uv run pytest tests/auth/integration
Skip this tier (no Docker): uv run pytest --ignore=tests/auth/integration
"""

from __future__ import annotations
```

`tests/auth/integration/test_ldap_integration.py`:
```python
"""LDAP integration tests against a real bitnami/openldap container."""

from __future__ import annotations


def test_collection_smoke():
    """Placeholder: pytest can collect tests under tests/auth/integration."""
    assert True
```

`tests/auth/integration/test_oauth_integration.py`:
```python
"""OAuth integration tests against a real Keycloak container."""

from __future__ import annotations


def test_collection_smoke():
    """Placeholder: pytest can collect tests under tests/auth/integration."""
    assert True
```

`tests/auth/integration/seed/.gitkeep`:
```
```
(empty)

- [ ] **Step 2: Verify collection works and basenames are unique**

```bash
uv run pytest tests/auth/integration --collect-only -q
```

Expected output includes `test_collection_smoke` from both files; no `ImportPathMismatchError` (which would indicate a basename collision).

- [ ] **Step 3: Run the placeholders**

```bash
uv run pytest tests/auth/integration -q
```

Expected: 2 passed.

- [ ] **Step 4: Verify no `__init__.py` was accidentally added**

```bash
test ! -f tests/auth/integration/__init__.py && echo "OK: no __init__.py"
```

Expected: `OK: no __init__.py`. Per CLAUDE.md, `--import-mode=importlib` requires `tests/` to NOT be a package.

- [ ] **Step 5: Commit**

```bash
git add tests/auth/integration/
git commit -m "test(auth): scaffold tests/auth/integration/ tier"
```

---

## Task 3: Implement `_tls.py` — generate CA + leaf cert

**Files:**
- Create: `tests/auth/integration/_tls.py`
- Test: `tests/auth/integration/test_ldap_integration.py` (add a TLS-helper smoke test)

- [ ] **Step 1: Write the failing test**

Add to `tests/auth/integration/test_ldap_integration.py`:

```python
import ssl
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding


def test_tls_helper_generates_valid_chain(tmp_path):
    """generate_ca_and_leaf produces a leaf cert signed by the CA, with
    SANs covering localhost + 127.0.0.1, in PEM files we can load."""
    from tests.auth.integration._tls import generate_ca_and_leaf

    paths = generate_ca_and_leaf(tmp_path)

    ca_pem = paths.ca_pem.read_bytes()
    server_pem = paths.server_pem.read_bytes()
    server_key = paths.server_key.read_bytes()

    ca_cert = x509.load_pem_x509_certificate(ca_pem)
    server_cert = x509.load_pem_x509_certificate(server_pem)

    # Leaf is signed by CA: verify the signature with CA's public key.
    ca_cert.public_key().verify(  # type: ignore[union-attr]
        server_cert.signature,
        server_cert.tbs_certificate_bytes,
        padding.PKCS1v15(),
        server_cert.signature_hash_algorithm,  # type: ignore[arg-type]
    )

    # SANs include localhost (DNS) and 127.0.0.1 (IP).
    san_ext = server_cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    )
    dns_names = san_ext.value.get_values_for_type(x509.DNSName)
    ip_addresses = [str(ip) for ip in san_ext.value.get_values_for_type(x509.IPAddress)]
    assert "localhost" in dns_names
    assert "127.0.0.1" in ip_addresses

    # ssl.SSLContext can load both as a cert chain (proves PEM format is valid).
    ctx = ssl.create_default_context(cafile=str(paths.ca_pem))
    ctx.load_cert_chain(certfile=str(paths.server_pem), keyfile=str(paths.server_key))

    # Key file is mode 0600.
    import stat
    assert stat.S_IMODE(paths.server_key.stat().st_mode) == 0o600
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py::test_tls_helper_generates_valid_chain -v
```

Expected: ImportError or ModuleNotFoundError on `tests.auth.integration._tls`.

- [ ] **Step 3: Implement `_tls.py`**

`tests/auth/integration/_tls.py`:

```python
"""Generate a self-signed CA + leaf cert for integration tests.

Pure cryptography, no openssl shell-out. The same leaf cert serves both
OpenLDAP (LDAPS) and Keycloak (HTTPS) since both bind to localhost:<random-port>.
"""

from __future__ import annotations

import datetime
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


@dataclass(frozen=True)
class TLSPaths:
    ca_pem: Path
    server_pem: Path
    server_key: Path


def generate_ca_and_leaf(target_dir: Path) -> TLSPaths:
    """Generate a CA + leaf cert into target_dir.

    Returns:
        TLSPaths with absolute paths to ca.pem, server.pem, server.key.
        server.key has mode 0600.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(minutes=5)
    not_after = now + datetime.timedelta(days=365)

    # CA
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "iris-test-ca"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    # Leaf (CN=localhost, SAN=DNS:localhost,IP:127.0.0.1)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
    ])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_subject)
        .issuer_name(ca_subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = target_dir / "ca.pem"
    server_pem = target_dir / "server.pem"
    server_key_path = target_dir / "server.key"

    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_pem.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    server_key_path.write_bytes(
        server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    server_key_path.chmod(0o600)

    return TLSPaths(
        ca_pem=ca_pem.resolve(),
        server_pem=server_pem.resolve(),
        server_key=server_key_path.resolve(),
    )
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py::test_tls_helper_generates_valid_chain -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/auth/integration/_tls.py tests/auth/integration/test_ldap_integration.py
git commit -m "test(auth): TLS helper for integration-test cert generation"
```

---

## Task 4: Add the `tls_paths` session fixture

**Files:**
- Modify: `tests/auth/integration/conftest.py`

- [ ] **Step 1: Add the fixture**

Append to `tests/auth/integration/conftest.py`:

```python
import pytest

from tests.auth.integration._tls import TLSPaths, generate_ca_and_leaf


@pytest.fixture(scope="session")
def tls_paths(tmp_path_factory) -> TLSPaths:
    """Generate a CA + leaf cert once per pytest session.

    The same paths are consumed by:
    - openldap_container (mounted as LDAPS cert + key + CA file)
    - keycloak_container (mounted as HTTPS cert + key)
    - LDAPProvider via LDAP_CA_CERT_PATH
    - OAuthProvider via OIDC_CA_CERT_PATH
    """
    target = tmp_path_factory.mktemp("auth-certs")
    return generate_ca_and_leaf(target)
```

- [ ] **Step 2: Add a fixture-consumes-fixture smoke test**

Add to `tests/auth/integration/test_ldap_integration.py`:

```python
def test_tls_paths_fixture_yields_resolved_paths(tls_paths):
    """The session-scoped tls_paths fixture yields absolute, resolved paths."""
    assert tls_paths.ca_pem.is_absolute()
    assert tls_paths.ca_pem.exists()
    assert tls_paths.server_pem.exists()
    assert tls_paths.server_key.exists()
```

- [ ] **Step 3: Run**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py -v
```

Expected: all tests pass (including the existing `test_collection_smoke`).

- [ ] **Step 4: Commit**

```bash
git add tests/auth/integration/conftest.py tests/auth/integration/test_ldap_integration.py
git commit -m "test(auth): add session-scoped tls_paths fixture"
```

---

## Task 5: Author the LDAP seed file

**Files:**
- Create: `tests/auth/integration/seed/ldap.ldif`

- [ ] **Step 1: Write the LDIF**

`tests/auth/integration/seed/ldap.ldif`:

```ldif
# OUs
dn: ou=people,dc=corp,dc=local
objectClass: organizationalUnit
ou: people

dn: ou=groups,dc=corp,dc=local
objectClass: organizationalUnit
ou: groups

# Users
dn: uid=alice,ou=people,dc=corp,dc=local
objectClass: inetOrgPerson
uid: alice
cn: Alice
sn: Alice
userPassword: secret

dn: uid=bob,ou=people,dc=corp,dc=local
objectClass: inetOrgPerson
uid: bob
cn: Bob
sn: Bob
userPassword: hunter2

dn: uid=carol,ou=people,dc=corp,dc=local
objectClass: inetOrgPerson
uid: carol
cn: Carol
sn: Carol
userPassword: carolpw

# Groups (groupOfNames — LDAPProvider's _read_groups searches member=DN)
dn: cn=admins,ou=groups,dc=corp,dc=local
objectClass: groupOfNames
cn: admins
member: uid=alice,ou=people,dc=corp,dc=local

dn: cn=users,ou=groups,dc=corp,dc=local
objectClass: groupOfNames
cn: users
member: uid=alice,ou=people,dc=corp,dc=local
member: uid=bob,ou=people,dc=corp,dc=local
```

- [ ] **Step 2: Validate LDIF syntax with a quick parse**

```bash
uv run python -c "
from ldif import LDIFParser
with open('tests/auth/integration/seed/ldap.ldif', 'rb') as f:
    entries = list(LDIFParser(f).parse())
print(f'OK: {len(entries)} entries')
"
```

If `python-ldif` is not installed, skip this step — the bitnami/openldap container itself will reject malformed LDIF at boot, and the next task will catch any errors.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/seed/ldap.ldif
git rm tests/auth/integration/seed/.gitkeep
git commit -m "test(auth): add OpenLDAP seed LDIF (alice/bob/carol + admins/users groups)"
```

---

## Task 6: Implement the `openldap_container` session fixture

**Files:**
- Modify: `tests/auth/integration/conftest.py`
- Test: `tests/auth/integration/test_ldap_integration.py`

- [ ] **Step 1: Write the failing smoke test**

Add to `tests/auth/integration/test_ldap_integration.py`:

```python
import ssl

from ldap3 import Connection, Server, Tls


def test_openldap_container_serves_ldaps_with_seeded_directory(
    openldap_container, tls_paths
):
    """The fixture starts OpenLDAP, mounts the LDIF seed, and serves LDAPS
    using the generated CA. We verify by binding as alice and reading her cn."""
    tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=str(tls_paths.ca_pem))
    server = Server(openldap_container.ldaps_url, get_info="NO_INFO", tls=tls)
    conn = Connection(
        server,
        user="uid=alice,ou=people,dc=corp,dc=local",
        password="secret",
        auto_bind=True,
    )
    conn.search(
        "uid=alice,ou=people,dc=corp,dc=local",
        "(objectClass=*)",
        attributes=["cn"],
    )
    assert conn.entries
    assert str(conn.entries[0].cn.value) == "Alice"
    conn.unbind()
```

- [ ] **Step 2: Run to confirm it fails**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py::test_openldap_container_serves_ldaps_with_seeded_directory -v
```

Expected: error — `openldap_container` fixture not defined.

- [ ] **Step 3: Implement the fixture**

Append to `tests/auth/integration/conftest.py`:

```python
from dataclasses import dataclass
from pathlib import Path

from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs


@dataclass(frozen=True)
class OpenLDAPHandle:
    host: str
    ldaps_port: int
    ldap_port: int

    @property
    def ldaps_url(self) -> str:
        return f"ldaps://{self.host}:{self.ldaps_port}"

    @property
    def ldap_url(self) -> str:
        return f"ldap://{self.host}:{self.ldap_port}"


@pytest.fixture(scope="session")
def openldap_container(tls_paths):
    """One bitnami/openldap container per session, seeded from ldap.ldif.

    Serves both ldaps:// (port 1636) and ldap:// (port 1389) — the latter is
    available so a TLS-required-but-plain-URL config error can be exercised
    without rebuilding the container.
    """
    seed_dir = Path(__file__).parent / "seed"
    ldif_dir = seed_dir.resolve()
    cert_dir = tls_paths.ca_pem.parent

    # Bitnami's image expects certs under one directory; we mount the cert
    # directory and reference filenames inside it.
    container = (
        DockerContainer("bitnami/openldap:2.6")
        .with_env("LDAP_ROOT", "dc=corp,dc=local")
        .with_env("LDAP_ADMIN_USERNAME", "admin")
        .with_env("LDAP_ADMIN_PASSWORD", "adminpw")
        .with_env("LDAP_CUSTOM_LDIF_DIR", "/ldifs")
        .with_env("LDAP_ENABLE_TLS", "yes")
        .with_env("LDAP_LDAPS_PORT_NUMBER", "1636")
        .with_env("LDAP_PORT_NUMBER", "1389")
        .with_env("LDAP_TLS_CERT_FILE", "/certs/server.pem")
        .with_env("LDAP_TLS_KEY_FILE", "/certs/server.key")
        .with_env("LDAP_TLS_CA_FILE", "/certs/ca.pem")
        # Skip the bitnami "skip default tree" so the image doesn't auto-create
        # a 'users' group that conflicts with our LDIF.
        .with_env("LDAP_SKIP_DEFAULT_TREE", "yes")
        .with_volume_mapping(str(ldif_dir), "/ldifs", "ro")
        .with_volume_mapping(str(cert_dir), "/certs", "ro")
        .with_exposed_ports(1636, 1389)
    )
    with container as c:
        # Wait for the bitnami image's "ready" log line. If the LDIF is bad,
        # the container exits before this fires; testcontainers raises with
        # the container logs attached.
        wait_for_logs(c, "slapd starting", timeout=30)
        host = c.get_container_host_ip()
        yield OpenLDAPHandle(
            host=host,
            ldaps_port=int(c.get_exposed_port(1636)),
            ldap_port=int(c.get_exposed_port(1389)),
        )
```

- [ ] **Step 4: Run the smoke test**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py::test_openldap_container_serves_ldaps_with_seeded_directory -v
```

Expected: PASS within ~15s.

If it fails with "wait_for_logs timeout", inspect the container logs:
```bash
uv run pytest tests/auth/integration/test_ldap_integration.py -v 2>&1 | grep -A 50 "stderr"
```
Common causes: typo in LDIF, wrong cert paths, port collision. Fix and re-run.

- [ ] **Step 5: Commit**

```bash
git add tests/auth/integration/conftest.py tests/auth/integration/test_ldap_integration.py
git commit -m "test(auth): session-scoped openldap_container fixture"
```

---

## Task 7: Author the Keycloak realm-import JSON

**Files:**
- Create: `tests/auth/integration/seed/keycloak-realm.json`

- [ ] **Step 1: Write the realm export**

`tests/auth/integration/seed/keycloak-realm.json`:

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
      "firstName": "Bob",
      "lastName": "Example",
      "credentials": [
        {"type": "password", "value": "hunter2", "temporary": false}
      ],
      "groups": ["/users"]
    }
  ],
  "groups": [
    {"name": "admins"},
    {"name": "users"}
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

- [ ] **Step 2: Validate JSON is well-formed**

```bash
uv run python -c "import json; json.load(open('tests/auth/integration/seed/keycloak-realm.json')); print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/seed/keycloak-realm.json
git commit -m "test(auth): add Keycloak realm-import JSON (iris-test realm with groups mapper)"
```

---

## Task 8: Implement the `keycloak_container` session fixture

**Files:**
- Modify: `tests/auth/integration/conftest.py`
- Test: `tests/auth/integration/test_oauth_integration.py`

- [ ] **Step 1: Write the failing smoke test**

Add to `tests/auth/integration/test_oauth_integration.py`:

```python
import httpx


def test_keycloak_container_serves_oidc_discovery(keycloak_container, tls_paths):
    """The fixture starts Keycloak with HTTPS + the realm imported, and
    discovery returns a valid OIDC document with the expected endpoints."""
    issuer = f"{keycloak_container.https_url}/realms/iris-test"
    discovery_url = f"{issuer}/.well-known/openid-configuration"

    with httpx.Client(verify=str(tls_paths.ca_pem), timeout=10.0) as http:
        r = http.get(discovery_url)
    r.raise_for_status()
    doc = r.json()

    assert doc["issuer"] == issuer
    assert doc["authorization_endpoint"].startswith(issuer)
    assert doc["token_endpoint"].startswith(issuer)
    assert "openid" in doc.get("scopes_supported", [])
```

- [ ] **Step 2: Run to confirm it fails**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py::test_keycloak_container_serves_oidc_discovery -v
```

Expected: error — `keycloak_container` fixture not defined.

- [ ] **Step 3: Implement the fixture**

Append to `tests/auth/integration/conftest.py`. **First** check whether `testcontainers.keycloak` is available (Task 1 verified this); if not, use the generic-DockerContainer fallback shown in the comment.

```python
@dataclass(frozen=True)
class KeycloakHandle:
    host: str
    https_port: int

    @property
    def https_url(self) -> str:
        return f"https://{self.host}:{self.https_port}"


@pytest.fixture(scope="session")
def keycloak_container(tls_paths):
    """One Keycloak container per session, with iris-test realm imported and
    HTTPS served using the generated leaf cert.

    Boot is the slowest step in the suite (~25s). Session-scoped so the cost
    is paid once per pytest invocation.
    """
    realm_json = (
        Path(__file__).parent / "seed" / "keycloak-realm.json"
    ).resolve()
    cert_dir = tls_paths.ca_pem.parent

    container = (
        DockerContainer("quay.io/keycloak/keycloak:26.0")
        .with_env("KC_BOOTSTRAP_ADMIN_USERNAME", "admin")
        .with_env("KC_BOOTSTRAP_ADMIN_PASSWORD", "admin")
        .with_env("KC_HTTPS_CERTIFICATE_FILE", "/certs/server.pem")
        .with_env("KC_HTTPS_CERTIFICATE_KEY_FILE", "/certs/server.key")
        .with_env("KC_HTTP_ENABLED", "false")
        .with_env("KC_HOSTNAME_STRICT", "false")
        .with_env("KC_HEALTH_ENABLED", "true")
        .with_volume_mapping(str(realm_json), "/opt/keycloak/data/import/realm.json", "ro")
        .with_volume_mapping(str(cert_dir), "/certs", "ro")
        .with_command("start-dev --import-realm")
        .with_exposed_ports(8443)
    )
    with container as c:
        # Keycloak prints "Listening on: https://0.0.0.0:8443" once HTTPS
        # is up and the realm has been imported. Timeout generously — cold
        # JVM start can take ~30s.
        wait_for_logs(c, "Listening on:", timeout=120)
        host = c.get_container_host_ip()
        yield KeycloakHandle(
            host=host,
            https_port=int(c.get_exposed_port(8443)),
        )
```

**Note:** if Task 1 found that `testcontainers.keycloak.KeycloakContainer` IS available, prefer it for boot/health waiting. The above generic-container path is the fallback. Both shapes return a handle with `.https_url`.

- [ ] **Step 4: Run the smoke test**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py::test_keycloak_container_serves_oidc_discovery -v
```

Expected: PASS within ~45s on first cold pull, ~30s afterward.

- [ ] **Step 5: Commit**

```bash
git add tests/auth/integration/conftest.py tests/auth/integration/test_oauth_integration.py
git commit -m "test(auth): session-scoped keycloak_container fixture"
```

---

## Task 9: Add `OIDC_CA_CERT_PATH` to OIDCSettings + wire into OAuthProvider

**Files:**
- Modify: `src/iris/auth/config.py`
- Modify: `src/iris/auth/providers/oauth.py`
- Test: `tests/auth/test_provider_oauth.py` (regression — existing tests must still pass; add one new test for the default-None case)

- [ ] **Step 1: Write the failing test for the new field default**

Add to `tests/auth/test_provider_oauth.py` (anywhere near the existing settings fixture):

```python
def test_oidc_settings_ca_cert_path_defaults_to_none():
    """OIDCSettings should have an optional ca_cert_path field; existing
    callers that don't pass it should get None."""
    s = OIDCSettings(
        issuer_url="https://example",
        client_id="x",
        client_secret="y",
        scopes=("openid",),
    )
    assert s.ca_cert_path is None
```

- [ ] **Step 2: Run to confirm it fails**

```bash
uv run pytest tests/auth/test_provider_oauth.py::test_oidc_settings_ca_cert_path_defaults_to_none -v
```

Expected: TypeError — `OIDCSettings.__init__()` got an unexpected keyword argument… actually NO arguments are unexpected; the existing test would just AttributeError on the assertion. Either way, fail.

- [ ] **Step 3: Add the field to OIDCSettings**

Modify `src/iris/auth/config.py`:

Replace:
```python
@dataclass(frozen=True)
class OIDCSettings:
    issuer_url: str
    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
```

with:

```python
@dataclass(frozen=True)
class OIDCSettings:
    issuer_url: str
    client_id: str
    client_secret: str
    scopes: tuple[str, ...]
    ca_cert_path: str | None = None
```

Then in `from_env()`, find the OIDC branch and replace:

```python
            oidc = OIDCSettings(
                issuer_url=_get_required("OIDC_ISSUER_URL"),
                client_id=_get_required("OIDC_CLIENT_ID"),
                client_secret=_get_required("OIDC_CLIENT_SECRET"),
                scopes=_split_ws(os.environ.get("OIDC_SCOPES", "openid profile email groups")),
            )
```

with:

```python
            oidc = OIDCSettings(
                issuer_url=_get_required("OIDC_ISSUER_URL"),
                client_id=_get_required("OIDC_CLIENT_ID"),
                client_secret=_get_required("OIDC_CLIENT_SECRET"),
                scopes=_split_ws(os.environ.get("OIDC_SCOPES", "openid profile email groups")),
                ca_cert_path=os.environ.get("OIDC_CA_CERT_PATH") or None,
            )
```

- [ ] **Step 4: Wire ca_cert_path into OAuthProvider**

Modify `src/iris/auth/providers/oauth.py`. Replace the body of `__init__` from:

```python
    def __init__(
        self,
        settings: OIDCSettings,
        *,
        _http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._client = httpx.Client(transport=_http_transport, timeout=10.0)
        # httpx.MockTransport (used in tests) implements both sync and async
        # dispatch but only inherits from BaseTransport, so we cast for the
        # async client. Real production code passes None and gets the default.
        self._async_client = httpx.AsyncClient(
            transport=cast("httpx.AsyncBaseTransport | None", _http_transport),
            timeout=10.0,
        )
```

with:

```python
    def __init__(
        self,
        settings: OIDCSettings,
        *,
        _http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        # When _http_transport is set (offline tests), the transport replaces
        # httpx's network stack entirely and `verify` is irrelevant. When it's
        # None (production + integration tests), we honor settings.ca_cert_path
        # so an internal/private CA can sign the IdP cert.
        verify_arg: bool | str = (
            settings.ca_cert_path if settings.ca_cert_path else True
        )
        if _http_transport is not None:
            self._client = httpx.Client(transport=_http_transport, timeout=10.0)
            # httpx.MockTransport implements both sync and async dispatch but
            # only inherits from BaseTransport; cast for the async client.
            self._async_client = httpx.AsyncClient(
                transport=cast("httpx.AsyncBaseTransport | None", _http_transport),
                timeout=10.0,
            )
        else:
            self._client = httpx.Client(verify=verify_arg, timeout=10.0)
            self._async_client = httpx.AsyncClient(verify=verify_arg, timeout=10.0)
```

- [ ] **Step 5: Run the new test + the full offline OAuth suite**

```bash
uv run pytest tests/auth/test_provider_oauth.py -v
```

Expected: all tests pass, including the new `test_oidc_settings_ca_cert_path_defaults_to_none` and all preexisting tests (the offline tests pass `_http_transport`, so the new branch is not exercised — they should still pass without change).

- [ ] **Step 6: Run pyright + ruff to catch type/lint regressions**

```bash
uv run basedpyright --level error
uv run ruff check
```

Expected: zero errors. (CLAUDE.md mandates pyright at zero errors AND zero warnings; if a warning surfaces, fix it before commit.)

```bash
uv run basedpyright --level warning
```

Expected: zero warnings.

- [ ] **Step 7: Commit**

```bash
git add src/iris/auth/config.py src/iris/auth/providers/oauth.py tests/auth/test_provider_oauth.py
git commit -m "feat(auth): OIDC_CA_CERT_PATH for OIDC over private CA"
```

---

## Task 10: Smoke-test that OAuthProvider can discover Keycloak with `ca_cert_path`

**Files:**
- Test: `tests/auth/integration/test_oauth_integration.py`

- [ ] **Step 1: Add the smoke test**

Add to `tests/auth/integration/test_oauth_integration.py`:

```python
from iris.auth.config import OIDCSettings
from iris.auth.providers.oauth import OAuthProvider


def test_oauth_provider_discovers_against_real_keycloak(
    keycloak_container, tls_paths
):
    """OAuthProvider with OIDC_CA_CERT_PATH set can discover endpoints
    against a self-signed Keycloak."""
    settings = OIDCSettings(
        issuer_url=f"{keycloak_container.https_url}/realms/iris-test",
        client_id="iris",
        client_secret="iris-test-secret",
        scopes=("openid", "profile", "email", "groups"),
        ca_cert_path=str(tls_paths.ca_pem),
    )
    provider = OAuthProvider(settings)
    try:
        # Property access triggers _ensure_discovered().
        assert provider.authorize_endpoint.startswith(settings.issuer_url)
        assert provider.token_endpoint.startswith(settings.issuer_url)
        assert provider.userinfo_endpoint.startswith(settings.issuer_url)
    finally:
        import asyncio
        asyncio.run(provider.close())
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py::test_oauth_provider_discovers_against_real_keycloak -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_oauth_integration.py
git commit -m "test(auth): smoke-test OAuthProvider discovery against real Keycloak"
```

---

## Task 11: Implement the Keycloak login-form helper

**Files:**
- Create: `tests/auth/integration/_keycloak_helpers.py`
- Test: `tests/auth/integration/test_oauth_integration.py`

- [ ] **Step 1: Write the failing helper smoke test**

Add to `tests/auth/integration/test_oauth_integration.py`:

```python
def test_simulate_login_drives_authorize_to_callback(
    keycloak_container, tls_paths, monkeypatch
):
    """The helper drives the full OAuth code flow against real Keycloak and
    returns the iris-side response holding the iris_session cookie."""
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv(
        "OIDC_ISSUER_URL",
        f"{keycloak_container.https_url}/realms/iris-test",
    )
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "iris-test-secret")
    monkeypatch.setenv("OIDC_SCOPES", "openid profile email groups")
    monkeypatch.setenv("OIDC_CA_CERT_PATH", str(tls_paths.ca_pem))

    from fastapi.testclient import TestClient
    from iris.app import build_app

    from tests.auth.integration._keycloak_helpers import simulate_login

    app = build_app()
    test_client = TestClient(app)
    with httpx.Client(verify=str(tls_paths.ca_pem), follow_redirects=False, timeout=10.0) as http:
        response = simulate_login(
            test_client=test_client, http=http, username="alice", password="secret"
        )
    assert response.status_code == 302
    # iris_session cookie should now be set
    assert response.cookies.get("iris_session") is not None
```

- [ ] **Step 2: Run to confirm it fails**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py::test_simulate_login_drives_authorize_to_callback -v
```

Expected: ImportError on `_keycloak_helpers`.

- [ ] **Step 3: Implement the helper**

`tests/auth/integration/_keycloak_helpers.py`:

```python
"""Drive Keycloak's authorize → login form → callback dance from a test.

TestClient can't follow a redirect to a different host, so the OAuth route
flow has to be split: TestClient handles the iris-side hops, a real httpx
client handles the Keycloak-side hops (login page GET + form POST).

The form-action regex is the only place that's coupled to Keycloak's login
HTML. A future Keycloak major bump that changes the layout is a one-line fix
here.
"""

from __future__ import annotations

import re

import httpx
from fastapi.testclient import TestClient

_FORM_ACTION_RE = re.compile(r'<form[^>]*\baction="([^"]+)"', re.IGNORECASE)


def _extract_form_action(html: str) -> str:
    m = _FORM_ACTION_RE.search(html)
    if not m:
        raise AssertionError(
            "Could not find <form action=\"...\"> in Keycloak login page. "
            "The login template may have changed; update _FORM_ACTION_RE."
        )
    # Keycloak renders the action with HTML entities (&amp;); decode them.
    return m.group(1).replace("&amp;", "&")


def simulate_login(
    *,
    test_client: TestClient,
    http: httpx.Client,
    username: str,
    password: str,
) -> httpx.Response:
    """Drive the full Authorization Code flow against a real Keycloak.

    Returns the iris-side response that has just received the callback —
    the same response a browser would see at the end of the redirect chain.

    Raises AssertionError on any unexpected HTTP behavior so failures
    surface as clear test errors, not opaque KeyError.
    """
    # 1. iris -> Keycloak: 302 to authorize endpoint, sets oauth_state cookie
    r = test_client.get("/login", follow_redirects=False)
    if r.status_code != 302 or "location" not in r.headers:
        raise AssertionError(
            f"Expected 302 from /login, got {r.status_code}: {r.text[:200]}"
        )
    authorize_url = r.headers["location"]

    # 2. user-agent visits the Keycloak login page
    page = http.get(authorize_url)
    if page.status_code != 200:
        raise AssertionError(
            f"Expected 200 from authorize page, got {page.status_code}"
        )
    form_action = _extract_form_action(page.text)

    # 3. POST credentials; Keycloak responds with 302 to our callback
    submit = http.post(
        form_action,
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    if submit.status_code != 302:
        raise AssertionError(
            "Expected 302 from Keycloak login form (got "
            f"{submit.status_code}). If 200, Keycloak rendered the login "
            "page again — check the credentials."
        )
    callback_url = submit.headers["location"]
    if "code=" not in callback_url:
        raise AssertionError(
            f"Keycloak redirect did not carry a `code` param: {callback_url}"
        )

    # 4. browser hits our callback with code+state, carrying the oauth_state cookie
    return test_client.get(callback_url, follow_redirects=False)
```

- [ ] **Step 4: Run the helper smoke test**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py::test_simulate_login_drives_authorize_to_callback -v
```

Expected: PASS.

If failure on step 3 (Keycloak responds 200, not 302): the form-action regex matched a wrong form, or credentials are wrong. Print `page.text[:1000]` to inspect.

- [ ] **Step 5: Commit**

```bash
git add tests/auth/integration/_keycloak_helpers.py tests/auth/integration/test_oauth_integration.py
git commit -m "test(auth): simulate_login helper drives Keycloak Authorization Code flow"
```

---

## Task 12: Add `ldap_app` and `oauth_app` per-test fixtures

**Files:**
- Modify: `tests/auth/integration/conftest.py`

- [ ] **Step 1: Add the fixtures**

Append to `tests/auth/integration/conftest.py`:

```python
from fastapi import FastAPI


@pytest.fixture
def ldap_app(monkeypatch, openldap_container, tls_paths) -> FastAPI:
    """A fresh iris app configured to authenticate against the openldap container.

    Uses monkeypatch.setenv to override the AUTH_METHOD=mock that
    tests/conftest.py set at module scope. Each test gets a freshly-built
    app via build_app(); env is restored after the test.
    """
    monkeypatch.setenv("AUTH_METHOD", "ldap")
    monkeypatch.setenv("LDAP_URL", openldap_container.ldaps_url)
    monkeypatch.setenv("LDAP_BIND_DN_TEMPLATE", "uid={username},ou=people,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_GROUP_BASE_DN", "ou=groups,dc=corp,dc=local")
    monkeypatch.setenv("LDAP_REQUIRE_TLS", "true")
    monkeypatch.setenv("LDAP_CA_CERT_PATH", str(tls_paths.ca_pem))

    from iris.app import build_app
    return build_app()


@pytest.fixture
def oauth_app(monkeypatch, keycloak_container, tls_paths) -> FastAPI:
    """A fresh iris app configured to authenticate against the keycloak container."""
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv(
        "OIDC_ISSUER_URL",
        f"{keycloak_container.https_url}/realms/iris-test",
    )
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "iris-test-secret")
    monkeypatch.setenv("OIDC_SCOPES", "openid profile email groups")
    monkeypatch.setenv("OIDC_CA_CERT_PATH", str(tls_paths.ca_pem))

    from iris.app import build_app
    return build_app()


@pytest.fixture
def keycloak_http(tls_paths):
    """A real httpx.Client that trusts the Keycloak self-signed cert.

    Used by simulate_login as the user-agent that visits Keycloak's login
    page. Lifetime: per-test, so cookies/state don't leak between tests.
    """
    with httpx.Client(
        verify=str(tls_paths.ca_pem), follow_redirects=False, timeout=10.0
    ) as client:
        yield client
```

Add `import httpx` near the top of the conftest if not already present.

- [ ] **Step 2: Add a fixture-construction smoke test**

Add to `tests/auth/integration/test_ldap_integration.py`:

```python
def test_ldap_app_fixture_builds(ldap_app):
    """The ldap_app fixture builds a FastAPI app configured with the LDAP provider.
    A simple GET /login should render the form (status 200)."""
    from fastapi.testclient import TestClient

    response = TestClient(ldap_app).get("/login")
    assert response.status_code == 200
    assert "name=\"username\"" in response.text or "name='username'" in response.text
```

- [ ] **Step 3: Run**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py::test_ldap_app_fixture_builds -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/auth/integration/conftest.py tests/auth/integration/test_ldap_integration.py
git commit -m "test(auth): per-test ldap_app/oauth_app/keycloak_http fixtures"
```

---

## Task 13: LDAP provider tests — happy paths (alice, bob, carol)

**Files:**
- Test: `tests/auth/integration/test_ldap_integration.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/auth/integration/test_ldap_integration.py`:

```python
import asyncio

from iris.auth.config import LDAPSettings
from iris.auth.providers.ldap import LDAPProvider


def _ldap_provider_for(openldap_container, tls_paths) -> LDAPProvider:
    return LDAPProvider(
        LDAPSettings(
            url=openldap_container.ldaps_url,
            bind_dn_template="uid={username},ou=people,dc=corp,dc=local",
            group_base_dn="ou=groups,dc=corp,dc=local",
            require_tls=True,
            ca_cert_path=str(tls_paths.ca_pem),
        )
    )


def test_provider_alice_has_admins_and_users_groups(openldap_container, tls_paths):
    provider = _ldap_provider_for(openldap_container, tls_paths)
    user = asyncio.run(provider.authenticate("alice", "secret"))
    assert user.subject == "uid=alice,ou=people,dc=corp,dc=local"
    assert user.username == "alice"
    assert user.display_name == "Alice"
    assert set(user.groups) == {"admins", "users"}


def test_provider_bob_has_only_users_group(openldap_container, tls_paths):
    provider = _ldap_provider_for(openldap_container, tls_paths)
    user = asyncio.run(provider.authenticate("bob", "hunter2"))
    assert set(user.groups) == {"users"}


def test_provider_carol_has_no_groups(openldap_container, tls_paths):
    """Carol exists in the directory but is not a member of any group:
    User.groups should be an empty tuple, not raise."""
    provider = _ldap_provider_for(openldap_container, tls_paths)
    user = asyncio.run(provider.authenticate("carol", "carolpw"))
    assert user.username == "carol"
    assert user.groups == ()
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py -v -k "provider_alice or provider_bob or provider_carol"
```

Expected: all three PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_ldap_integration.py
git commit -m "test(auth): LDAP provider happy-path integration tests"
```

---

## Task 14: LDAP provider tests — failure paths (bad password, unknown user)

**Files:**
- Test: `tests/auth/integration/test_ldap_integration.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/auth/integration/test_ldap_integration.py`:

```python
import pytest

from iris.auth.exceptions import AuthError


def test_provider_wrong_password_raises_invalid_credentials(
    openldap_container, tls_paths
):
    provider = _ldap_provider_for(openldap_container, tls_paths)
    with pytest.raises(AuthError) as exc_info:
        asyncio.run(provider.authenticate("alice", "WRONG"))
    assert exc_info.value.token == "invalid_credentials"


def test_provider_unknown_user_raises_invalid_credentials(
    openldap_container, tls_paths
):
    provider = _ldap_provider_for(openldap_container, tls_paths)
    with pytest.raises(AuthError) as exc_info:
        asyncio.run(provider.authenticate("nobody", "anything"))
    assert exc_info.value.token == "invalid_credentials"
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py -v -k "wrong_password or unknown_user"
```

Expected: both PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_ldap_integration.py
git commit -m "test(auth): LDAP provider wrong-creds + unknown-user integration tests"
```

---

## Task 15: LDAP provider TLS failure — wrong CA bundle

**Files:**
- Test: `tests/auth/integration/test_ldap_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/auth/integration/test_ldap_integration.py`:

```python
def test_provider_wrong_ca_raises_ldap_unreachable(
    openldap_container, tmp_path
):
    """Pointing LDAP_CA_CERT_PATH at a different CA than the one the
    server's cert chains to should make TLS verification fail. ldap3
    surfaces this as a generic LDAPException → AuthError('ldap_unreachable')."""
    from tests.auth.integration._tls import generate_ca_and_leaf

    bad_paths = generate_ca_and_leaf(tmp_path / "wrong-ca")
    bad_provider = LDAPProvider(
        LDAPSettings(
            url=openldap_container.ldaps_url,
            bind_dn_template="uid={username},ou=people,dc=corp,dc=local",
            group_base_dn="ou=groups,dc=corp,dc=local",
            require_tls=True,
            ca_cert_path=str(bad_paths.ca_pem),
        )
    )
    with pytest.raises(AuthError) as exc_info:
        asyncio.run(bad_provider.authenticate("alice", "secret"))
    assert exc_info.value.token == "ldap_unreachable"
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py::test_provider_wrong_ca_raises_ldap_unreachable -v
```

Expected: PASS. (LDAPProvider's `_open_connection` catches `LDAPSocketOpenError`/`LDAPException` and raises `_Unreachable`, which becomes `AuthError("ldap_unreachable")`.)

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_ldap_integration.py
git commit -m "test(auth): LDAP provider wrong-CA TLS rejection integration test"
```

---

## Task 16: LDAP route tests — happy + bad-password + unknown-user

**Files:**
- Test: `tests/auth/integration/test_ldap_integration.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/auth/integration/test_ldap_integration.py`:

```python
from fastapi.testclient import TestClient


def _login_form_csrf(client: TestClient) -> str:
    """GET /login, parse the CSRF token out of the rendered form."""
    r = client.get("/login")
    assert r.status_code == 200
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m is not None, "Login form did not render a csrf_token field"
    return m.group(1)


def test_route_login_alice_creates_session_with_admin_role(ldap_app):
    """POST /login alice/secret -> 302 + iris_session cookie; whoami works."""
    client = TestClient(ldap_app)
    csrf = _login_form_csrf(client)
    r = client.post(
        "/login",
        data={"username": "alice", "password": "secret", "next": "/", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 302, f"login failed: {r.status_code} {r.text[:300]}"
    assert r.cookies.get("iris_session") is not None

    # whoami via the session cookie
    me = client.get("/api/whoami")
    assert me.status_code == 200
    body = me.json()
    assert body["display_name"] == "Alice"
    assert set(body["groups"]) == {"admins", "users"}


def test_route_login_bad_password_redirects_with_error_token(ldap_app):
    client = TestClient(ldap_app)
    csrf = _login_form_csrf(client)
    r = client.post(
        "/login",
        data={"username": "alice", "password": "WRONG", "next": "/", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=invalid_credentials" in r.headers["location"]
    assert r.cookies.get("iris_session") is None


def test_route_login_unknown_user_redirects_with_error_token(ldap_app):
    client = TestClient(ldap_app)
    csrf = _login_form_csrf(client)
    r = client.post(
        "/login",
        data={"username": "nobody", "password": "anything", "next": "/", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "error=invalid_credentials" in r.headers["location"]
    assert r.cookies.get("iris_session") is None
```

Add `import re` at the top of the test file if not already present.

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_ldap_integration.py -v -k "route_login"
```

Expected: 3 PASS.

If `whoami` returns 401 with valid creds: the YAML role mapping wasn't applied. Check that `tests/conftest.py` populated `AUTHZ_CONFIG_PATH` (it does, at module scope) and that the file still exists. `whoami` only requires a valid session, not a specific role, so a 401 here would point at session/cookie plumbing, not role mapping.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_ldap_integration.py
git commit -m "test(auth): LDAP route-level login integration tests"
```

---

## Task 17: OAuth provider tests — happy paths (alice + bob via simulate_login)

**Files:**
- Test: `tests/auth/integration/test_oauth_integration.py`

- [ ] **Step 1: Write the helper that extracts a fresh code from Keycloak**

Add to `tests/auth/integration/_keycloak_helpers.py`:

```python
from urllib.parse import parse_qs, urlparse


def obtain_authorization_code(
    *,
    test_client: TestClient,
    http: httpx.Client,
    username: str,
    password: str,
) -> tuple[str, str]:
    """Drive Keycloak's authorize+login flow and return (code, state).

    Stops one step short of simulate_login: instead of re-entering iris's
    callback, returns the raw `code` and `state` so a provider-level test
    can call OAuthProvider.exchange_code() directly.

    Also returns the redirect_uri used in the authorize step so that an
    intentional redirect_uri-mismatch test can compare against it.
    """
    r = test_client.get("/login", follow_redirects=False)
    authorize_url = r.headers["location"]
    page = http.get(authorize_url)
    form_action = _extract_form_action(page.text)
    submit = http.post(
        form_action,
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    if submit.status_code != 302:
        raise AssertionError(
            f"Keycloak login did not redirect (status={submit.status_code})"
        )
    callback_url = submit.headers["location"]
    qs = parse_qs(urlparse(callback_url).query)
    return qs["code"][0], qs["state"][0]
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/auth/integration/test_oauth_integration.py`:

```python
import asyncio

from iris.auth.config import OIDCSettings
from iris.auth.providers.oauth import OAuthProvider

from tests.auth.integration._keycloak_helpers import obtain_authorization_code


def _oauth_provider(keycloak_container, tls_paths, *, client_secret="iris-test-secret") -> OAuthProvider:
    return OAuthProvider(
        OIDCSettings(
            issuer_url=f"{keycloak_container.https_url}/realms/iris-test",
            client_id="iris",
            client_secret=client_secret,
            scopes=("openid", "profile", "email", "groups"),
            ca_cert_path=str(tls_paths.ca_pem),
        )
    )


def test_provider_exchange_code_returns_alice_with_groups(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    """Drive Keycloak's authorize+login dance, then call exchange_code()
    directly: the User comes back with groups from the realm."""
    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http, username="alice", password="secret"
    )
    # The code was obtained during oauth_app's GET /login, where iris built
    # the authorize URL with redirect_uri=http://testserver/login/callback
    # (the realm's only registered redirect URI). exchange_code must use
    # that same redirect_uri.
    redirect_uri = "http://testserver/login/callback"

    provider = _oauth_provider(keycloak_container, tls_paths)
    try:
        # We need the verifier from the iris-side oauth_state cookie. Easier:
        # build a fresh authorize_url ourselves and use its verifier.
        # But the code we just got is bound to iris's verifier. Workaround:
        # grab the verifier from the iris cookie that the GET /login set.
        signed_state_cookie = test_client.cookies.get("oauth_state")
        from itsdangerous import URLSafeTimedSerializer
        signer = URLSafeTimedSerializer("iris-test-secret", salt="iris-oauth-state")
        payload = signer.loads(signed_state_cookie)
        verifier = payload["verifier"]

        user = asyncio.run(
            provider.exchange_code(
                code=code, code_verifier=verifier, redirect_uri=redirect_uri
            )
        )
    finally:
        asyncio.run(provider.close())

    assert user.username == "alice"
    assert user.display_name == "Alice"
    assert set(user.groups) == {"admins", "users"}


def test_provider_exchange_code_returns_bob_with_users_group(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http, username="bob", password="hunter2"
    )
    redirect_uri = "http://testserver/login/callback"

    signed_state_cookie = test_client.cookies.get("oauth_state")
    from itsdangerous import URLSafeTimedSerializer
    signer = URLSafeTimedSerializer("iris-test-secret", salt="iris-oauth-state")
    verifier = signer.loads(signed_state_cookie)["verifier"]

    provider = _oauth_provider(keycloak_container, tls_paths)
    try:
        user = asyncio.run(
            provider.exchange_code(code=code, code_verifier=verifier, redirect_uri=redirect_uri)
        )
    finally:
        asyncio.run(provider.close())
    assert user.username == "bob"
    assert set(user.groups) == {"users"}
```

- [ ] **Step 3: Run**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py -v -k "provider_exchange_code_returns"
```

Expected: 2 PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/auth/integration/_keycloak_helpers.py tests/auth/integration/test_oauth_integration.py
git commit -m "test(auth): OAuth provider exchange_code happy-path integration tests"
```

---

## Task 18: OAuth provider failure — wrong client_secret

**Files:**
- Test: `tests/auth/integration/test_oauth_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/auth/integration/test_oauth_integration.py`:

```python
import pytest

from iris.auth.exceptions import AuthError


def test_provider_wrong_client_secret_raises_oauth_exchange(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    """Building a provider with the wrong client_secret should make
    Keycloak reject the token exchange → AuthError('oauth_exchange')."""
    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http, username="alice", password="secret"
    )
    signed_state_cookie = test_client.cookies.get("oauth_state")
    from itsdangerous import URLSafeTimedSerializer
    signer = URLSafeTimedSerializer("iris-test-secret", salt="iris-oauth-state")
    verifier = signer.loads(signed_state_cookie)["verifier"]

    bad_provider = _oauth_provider(
        keycloak_container, tls_paths, client_secret="WRONG-SECRET"
    )
    try:
        with pytest.raises(AuthError) as exc:
            asyncio.run(
                bad_provider.exchange_code(
                    code=code,
                    code_verifier=verifier,
                    redirect_uri="http://testserver/login/callback",
                )
            )
        assert exc.value.token == "oauth_exchange"
    finally:
        asyncio.run(bad_provider.close())
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py::test_provider_wrong_client_secret_raises_oauth_exchange -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_oauth_integration.py
git commit -m "test(auth): OAuth provider wrong-client-secret integration test"
```

---

## Task 19: OAuth provider failure — redirect_uri mismatch

**Files:**
- Test: `tests/auth/integration/test_oauth_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/auth/integration/test_oauth_integration.py`:

```python
def test_provider_redirect_uri_mismatch_raises_oauth_exchange(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    """The code is obtained against http://testserver/login/callback;
    exchanging it with a different redirect_uri must be rejected by Keycloak."""
    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http, username="alice", password="secret"
    )
    signed_state_cookie = test_client.cookies.get("oauth_state")
    from itsdangerous import URLSafeTimedSerializer
    signer = URLSafeTimedSerializer("iris-test-secret", salt="iris-oauth-state")
    verifier = signer.loads(signed_state_cookie)["verifier"]

    provider = _oauth_provider(keycloak_container, tls_paths)
    try:
        with pytest.raises(AuthError) as exc:
            asyncio.run(
                provider.exchange_code(
                    code=code,
                    code_verifier=verifier,
                    redirect_uri="http://testserver/some-other-path",
                )
            )
        assert exc.value.token == "oauth_exchange"
    finally:
        asyncio.run(provider.close())
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py::test_provider_redirect_uri_mismatch_raises_oauth_exchange -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_oauth_integration.py
git commit -m "test(auth): OAuth provider redirect_uri-mismatch integration test"
```

---

## Task 20: OAuth provider failure — code reuse

**Files:**
- Test: `tests/auth/integration/test_oauth_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/auth/integration/test_oauth_integration.py`:

```python
def test_provider_code_reuse_raises_oauth_exchange_on_second_call(
    keycloak_container, tls_paths, oauth_app, keycloak_http
):
    """Keycloak invalidates an authorization code on first use. Reusing it
    must fail with AuthError('oauth_exchange')."""
    test_client = TestClient(oauth_app)
    code, _state = obtain_authorization_code(
        test_client=test_client, http=keycloak_http, username="alice", password="secret"
    )
    signed_state_cookie = test_client.cookies.get("oauth_state")
    from itsdangerous import URLSafeTimedSerializer
    signer = URLSafeTimedSerializer("iris-test-secret", salt="iris-oauth-state")
    verifier = signer.loads(signed_state_cookie)["verifier"]
    redirect_uri = "http://testserver/login/callback"

    provider = _oauth_provider(keycloak_container, tls_paths)
    try:
        # First call succeeds.
        user = asyncio.run(
            provider.exchange_code(code=code, code_verifier=verifier, redirect_uri=redirect_uri)
        )
        assert user.username == "alice"

        # Second call against the same code must fail.
        with pytest.raises(AuthError) as exc:
            asyncio.run(
                provider.exchange_code(
                    code=code, code_verifier=verifier, redirect_uri=redirect_uri
                )
            )
        assert exc.value.token == "oauth_exchange"
    finally:
        asyncio.run(provider.close())
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py::test_provider_code_reuse_raises_oauth_exchange_on_second_call -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_oauth_integration.py
git commit -m "test(auth): OAuth provider code-reuse integration test"
```

---

## Task 21: OAuth provider failure — wrong CA bundle on discovery

**Files:**
- Test: `tests/auth/integration/test_oauth_integration.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/auth/integration/test_oauth_integration.py`:

```python
def test_provider_wrong_ca_bundle_raises_oauth_discovery(
    keycloak_container, tmp_path
):
    """OAuthProvider configured with a CA bundle that doesn't include
    Keycloak's CA must fail discovery with AuthError('oauth_discovery')."""
    from tests.auth.integration._tls import generate_ca_and_leaf

    bad_ca = generate_ca_and_leaf(tmp_path / "wrong-ca")
    settings = OIDCSettings(
        issuer_url=f"{keycloak_container.https_url}/realms/iris-test",
        client_id="iris",
        client_secret="iris-test-secret",
        scopes=("openid",),
        ca_cert_path=str(bad_ca.ca_pem),
    )
    provider = OAuthProvider(settings)
    try:
        with pytest.raises(AuthError) as exc:
            _ = provider.authorize_endpoint  # triggers _ensure_discovered
        assert exc.value.token == "oauth_discovery"
    finally:
        asyncio.run(provider.close())
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py::test_provider_wrong_ca_bundle_raises_oauth_discovery -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_oauth_integration.py
git commit -m "test(auth): OAuth provider wrong-CA discovery-failure integration test"
```

---

## Task 22: OAuth route tests — alice (admin) + bob (no roles) end-to-end

**Files:**
- Test: `tests/auth/integration/test_oauth_integration.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/auth/integration/test_oauth_integration.py`:

```python
from tests.auth.integration._keycloak_helpers import simulate_login


def test_route_oauth_alice_full_flow_creates_session_with_admin_role(
    oauth_app, keycloak_http
):
    """End-to-end: authorize → Keycloak login → callback → iris session
    cookie set; whoami succeeds and groups include both admins and users."""
    client = TestClient(oauth_app)
    response = simulate_login(
        test_client=client, http=keycloak_http, username="alice", password="secret"
    )
    assert response.status_code == 302
    assert response.cookies.get("iris_session") is not None

    me = client.get("/api/whoami")
    assert me.status_code == 200
    body = me.json()
    assert body["display_name"] == "Alice"
    assert set(body["groups"]) == {"admins", "users"}


def test_route_oauth_bob_full_flow_creates_session(oauth_app, keycloak_http):
    """Bob is only in 'users'; the session is still created (the role
    YAML at tests/conftest.py doesn't map 'users' to a role, but bob is
    still a valid authenticated user)."""
    client = TestClient(oauth_app)
    response = simulate_login(
        test_client=client, http=keycloak_http, username="bob", password="hunter2"
    )
    assert response.status_code == 302
    assert response.cookies.get("iris_session") is not None

    me = client.get("/api/whoami")
    assert me.status_code == 200
    body = me.json()
    assert body["display_name"] == "Bob"
    assert set(body["groups"]) == {"users"}
```

- [ ] **Step 2: Run**

```bash
uv run pytest tests/auth/integration/test_oauth_integration.py -v -k "route_oauth"
```

Expected: 2 PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/auth/integration/test_oauth_integration.py
git commit -m "test(auth): OAuth route-level full-flow integration tests"
```

---

## Task 23: Document the integration tier in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add an "Integration test tier" section under the Tests heading**

Insert under the existing `### Tests` section in CLAUDE.md (the auth one, not the clickhouse one), after the existing fixture documentation:

```markdown
### Integration tests (`tests/auth/integration/`)

A second tier under `tests/auth/integration/` runs the LDAP and OAuth providers end-to-end against real `bitnami/openldap:2.6` and `quay.io/keycloak/keycloak:26.0` containers via `testcontainers-python`. Covers happy paths and natural failure paths exercisable against a real IdP (wrong CA, bad creds, code reuse, redirect_uri mismatch, wrong client secret) plus full TLS coverage for both providers. The existing offline tests under `tests/auth/test_provider_*.py` stay as the fast unit tier.

- Run only the integration tier: `uv run pytest tests/auth/integration`
- Skip the integration tier (no Docker required): `uv run pytest --ignore=tests/auth/integration`
- Runtime: ~45–60s on a warm cache (Keycloak boot dominates). Session-scoped containers amortize across the full integration suite.

Seed data lives in `tests/auth/integration/seed/` (`ldap.ldif`, `keycloak-realm.json`) — declarative, committed to git. TLS certs are generated at session start via `_tls.py` and not committed. The `_keycloak_helpers.simulate_login` helper drives Keycloak's authorize → login form → callback flow through `TestClient`; the form-action regex is the only place coupled to Keycloak's login HTML.
```

Also: update the env-var reference table in CLAUDE.md's `### Configuration` section. Find the OIDC block:

```
# OAuth (OIDC discovery)
OIDC_ISSUER_URL=https://keycloak.example.com/realms/iris
OIDC_CLIENT_ID=iris
OIDC_CLIENT_SECRET=...
OIDC_SCOPES=openid profile email groups
```

and add one line:

```
OIDC_CA_CERT_PATH=                     # optional: PEM bundle for IdP cert validation (private CA)
```

- [ ] **Step 2: Read back the relevant CLAUDE.md sections to verify they read coherently**

```bash
uv run grep -n "integration\|OIDC_CA_CERT_PATH" CLAUDE.md
```

Expected: the new lines appear in sensible contexts.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document the auth integration test tier and OIDC_CA_CERT_PATH"
```

---

## Task 24: Final verification — full test suite + lint + type-check

**Files:** none.

- [ ] **Step 1: Run the full test suite (offline + integration)**

```bash
uv run pytest -q
```

Expected: all tests pass. Note total time: should be ~5s (offline) + ~45–60s (integration) ≈ ~60s.

- [ ] **Step 2: Run ruff**

```bash
uv run ruff check
```

Expected: only the existing intentional `E402` in `src/iris/__init__.py`. No new issues.

- [ ] **Step 3: Run pyright at error and warning levels**

```bash
uv run basedpyright --level error
uv run basedpyright --level warning
```

Expected: zero errors, zero warnings.

If pyright surfaces unknown-type warnings on the new fixtures in `tests/auth/integration/conftest.py` (testcontainers, ldap3 are dynamic), follow the existing pattern in `tests/clickhouse/conftest.py` and add the appropriate file-level `# pyright:` suppression at the top of the conftest. Do NOT scatter `# type: ignore` comments through the test bodies.

- [ ] **Step 4: Confirm the offline tier still runs without Docker**

```bash
uv run pytest --ignore=tests/auth/integration -q
```

Expected: pre-integration test count (everything except the ~17 new tests) passes in ~5s.

- [ ] **Step 5: Commit any final pyright/ruff fixups (if any)**

```bash
# Only if Step 3 surfaced something needing a fix:
git add -p
git commit -m "test(auth): pyright suppressions for dynamic testcontainers fixtures"
```

---

## Self-Review

A pass over the plan with the spec open:

**Spec coverage:**
- §Layout (sub-package, no `__init__.py`, helper underscore prefixes) — Tasks 2, 11.
- §Containers + seed (bitnami/openldap, Keycloak 26.0, LDIF, realm JSON, groups mapper) — Tasks 5, 6, 7, 8.
- §TLS certs (CA + leaf in pure cryptography, SANs, 0600 key) — Task 3.
- §Per-test app construction (`monkeypatch.setenv` + `build_app()`) — Task 12.
- §Route-level OAuth orchestration (`simulate_login`, form-action regex) — Task 11.
- §Test plan: 6 LDAP provider + 3 LDAP route + 6 OAuth provider + 2 OAuth route = 17 — Tasks 13, 14, 15, 16, 17, 18, 19, 20, 21, 22 (alice + bob OAuth happy is one task with two tests = 2; same for LDAP carol + alice + bob = 3 in one task). Test count matches.
- §Per-test isolation (read-only, session-scoped containers) — Task 6, 8 fixtures.
- §Docker behavior (fail-loud, `--ignore=` opt-out) — Task 23.
- §Dependencies (`testcontainers[keycloak]`, with fallback) — Task 1.

**Spec gap discovered during planning:** the spec said "no production-code change needed" for OAuth TLS, but `OAuthProvider`'s sync+async client construction can't accept a single `httpx.AsyncHTTPTransport` cleanly. The plan adds `OIDCSettings.ca_cert_path` + `OIDC_CA_CERT_PATH` env var (Task 9) — a small, production-correct change that mirrors `LDAP_CA_CERT_PATH`. CLAUDE.md is updated to document the new env var (Task 23).

**Placeholder scan:** no "TBD" / "implement later" / "similar to Task N" in any step. All code blocks are complete.

**Type consistency:** `TLSPaths` defined in Task 3 is referenced consistently in Tasks 4, 6, 8, 9, 10, 13–22. `OpenLDAPHandle` (`.ldaps_url`, `.ldap_url`) and `KeycloakHandle` (`.https_url`) defined in Tasks 6 and 8 are used consistently in downstream tests. `OIDCSettings.ca_cert_path` defined in Task 9 is used identically in Tasks 10, 17, 18, 19, 20, 21.

**Scope:** 24 tasks, all in the same package boundary (`tests/auth/integration/` + small additions to `src/iris/auth/{config,providers/oauth}.py` + CLAUDE.md). Single implementation plan; no decomposition needed.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-05-auth-testcontainers.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
