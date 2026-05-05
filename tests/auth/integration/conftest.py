"""Fixtures for the auth integration tier (Keycloak via real container).

Spins up Keycloak via testcontainers-python, generates a self-signed CA + leaf
cert in pure Python, and yields per-test FastAPI apps configured to use the
real OAuth provider.

LDAP integration was descoped in favor of focusing on the OAuth path; the
existing offline LDAP tests in tests/auth/test_provider_ldap.py remain.

This conftest layers on top of tests/conftest.py: the parent conftest sets
AUTH_METHOD=mock at module scope; integration tests use monkeypatch.setenv to
override that for the duration of the test.

Run only this tier:        uv run pytest tests/auth/integration
Skip this tier (no Docker): uv run pytest --ignore=tests/auth/integration
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from tests.auth.integration._tls import TLSPaths, generate_ca_and_leaf


@pytest.fixture(scope="session")
def tls_paths(tmp_path_factory) -> TLSPaths:
    """Generate a CA + leaf cert once per pytest session.

    The same paths are consumed by:
    - keycloak_container (mounted as HTTPS cert + key)
    - OAuthProvider via OIDC_CA_CERT_PATH
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
    """One Keycloak container per session, with iris-test realm imported and
    HTTPS served using the generated leaf cert.

    Boot is the slowest step in the integration suite (~12s warm; ~30s on a
    cold Docker layer cache). Session-scoped so the cost is paid once per
    pytest invocation regardless of how many integration tests are selected.
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
        .with_env("KC_HOSTNAME_STRICT", "false")
        # Mount the realm JSON at the path Keycloak's --import-realm scans.
        .with_volume_mapping(
            str(realm_json),
            "/opt/keycloak/data/import/iris-test-realm.json",
            "ro",
        )
        .with_volume_mapping(str(cert_dir), "/certs", "ro")
        .with_command("start-dev --import-realm")
        .with_exposed_ports(8443)
    )
    with container as c:
        # Quarkus prints "Listening on: http://... and https://..." once both
        # the realm import is done and the HTTPS listener is up. Generous
        # timeout because cold JVM start can take ~30s on slower hosts.
        wait_for_logs(c, "Listening on:", timeout=120)
        host = c.get_container_host_ip()
        yield KeycloakHandle(
            host=host,
            https_port=int(c.get_exposed_port(8443)),
        )
