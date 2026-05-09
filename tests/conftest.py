from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from tests._tls import TLSPaths, generate_ca_and_leaf

# Test fixtures that the auth layer needs at import time. setdefault means
# a developer's real .env / shell env can still override these.
os.environ.setdefault("AUTH_METHOD", "mock")
os.environ.setdefault("MOCK_USERNAME", "alice")
os.environ.setdefault("MOCK_PASSWORD", "secret")
os.environ.setdefault("MOCK_GROUPS", "admins,users")
os.environ.setdefault("MOCK_DISPLAY_NAME", "Alice")
os.environ.setdefault("COOKIE_SECURE", "false")
# Sessions live in SQLite (the only thing left in AUTH_DB_PATH). One connection
# per process means :memory: works for single-process tests; multi-process
# tests use a tempfile.
os.environ.setdefault("AUTH_DB_PATH", ":memory:")


@pytest.fixture
def app():
    from iris.app import build_app

    return build_app(install_clickhouse=False)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def authed_client(app):
    from iris.auth.identity import User

    c = TestClient(app)
    store = app.state.auth_session_store
    user = User(
        subject="mock:alice",
        username="alice",
        display_name="Alice",
        groups=("admins", "users"),
    )
    session = asyncio.run(store.create(user))
    c.cookies.set("iris_session", session.id)
    return c


# ---------------------------------------------------------------------------
# Shared integration-tier fixtures (used by tests/auth/integration/ and
# tests/clickhouse/integration/). Promoted from tests/auth/integration/conftest.py
# so a single Keycloak container serves both suites.
# ---------------------------------------------------------------------------


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

    Realm JSON lives at ``tests/seed/keycloak-realm.json``; mounted into
    the container so Keycloak's ``--import-realm`` picks it up.
    """
    realm_json = (Path(__file__).parent / "seed" / "keycloak-realm.json").resolve()
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
