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

import re
import ssl
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from tests.auth.integration._tls import TLSPaths, generate_ca_and_leaf


def _ssl_context_trusting(ca_pem: Path) -> ssl.SSLContext:
    """SSLContext that verifies against the integration tier's self-signed CA."""
    return ssl.create_default_context(cafile=str(ca_pem))


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

    # Quarkus prints "Listening on: http://... and https://..." once both the
    # realm import is done and the HTTPS listener is up. Generous timeout
    # because cold JVM start can take ~30s on slower hosts.
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
        # Mount the realm JSON at the path Keycloak's --import-realm scans.
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


@pytest.fixture
def oauth_app(monkeypatch, keycloak_container, tls_paths) -> FastAPI:
    """A fresh iris app configured to authenticate against the Keycloak container.

    Uses monkeypatch.setenv to override the AUTH_METHOD=mock that
    tests/conftest.py set at module scope. Each test gets a freshly-built
    app via build_app(); env is restored after the test.
    """
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OIDC_ISSUER_URL", keycloak_container.issuer_url)
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "iris-test-secret")
    # The 'groups' claim is emitted by a client-level oidc-group-membership-mapper
    # (see seed/keycloak-realm.json), so we don't need a 'groups' client scope —
    # which Keycloak doesn't ship by default.
    monkeypatch.setenv("OIDC_SCOPES", "openid profile email")
    monkeypatch.setenv("OIDC_CA_CERT_PATH", str(tls_paths.ca_pem))
    # Keycloak issues redirect_uri checks against http://testserver/login/callback,
    # which is TestClient's default. Cookie-Secure must be off because TestClient
    # uses http://, not https://.
    monkeypatch.setenv("COOKIE_SECURE", "false")

    from iris.app import build_app
    return build_app()


@pytest.fixture
def keycloak_http(tls_paths):
    """A real httpx.Client that trusts the Keycloak self-signed cert.

    Used by simulate_login as the user-agent that visits Keycloak's login
    page. Defaults to follow_redirects=True (Keycloak typically 302s the
    authorize URL once before rendering the login form); the helper overrides
    per-call where capturing a specific 302 matters (the form POST).

    Lifetime: per-test, so cookies/state don't leak between tests.
    """
    with httpx.Client(
        verify=_ssl_context_trusting(tls_paths.ca_pem),
        follow_redirects=True,
        timeout=10.0,
    ) as client:
        yield client
