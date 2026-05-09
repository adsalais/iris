"""Fixtures for ClickHouse end-to-end integration tests.

Builds an iris app per test (``iris_app``) configured to authenticate
against the real Keycloak (``keycloak_container`` from
``tests/conftest.py``) and connect to the real CH testcontainer
(``ch_settings`` from ``tests/clickhouse/conftest.py``).

The ``provisioned_creators_grant`` fixture is autouse + session-scoped:
it grants ``CREATE DATABASE`` to ``creators_GRP`` once via a privileged
testcontainer admin client, so subsequent bob logins land with
``can_create_database=True``.
"""
from __future__ import annotations

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
    need to repeat the setup. Uses a privileged admin client (the same
    testcontainer's default user, via clickhouse-connect) rather than
    going through iris auth — this is test setup, not what we're testing.
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
