"""Fixtures for the ClickHouse test suite.

Spins up a real ClickHouse server in a Docker container once per pytest session,
populates the CLICKHOUSE_* env vars to point at it, and yields a `build_client`
result with the service admin already bootstrapped.

Tests should namespace any entities they create (users, roles, databases, tables)
with the ``prefix`` fixture, since state accumulates across tests within a session.
"""

from __future__ import annotations

import uuid

import pytest
from testcontainers.clickhouse import ClickHouseContainer

from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings


@pytest.fixture(scope="session")
def ch_container():
    """One ClickHouse server per test session."""
    container = ClickHouseContainer("clickhouse/clickhouse-server:24")
    with container as ch:
        yield ch


@pytest.fixture
def ch_settings(ch_container, monkeypatch):
    """ClickHouseSettings pointing at the running container.

    Each test gets a fresh ``from_env()`` so the dataclass is hermetic — the env
    overrides are scoped to the test by ``monkeypatch``.
    """
    host = ch_container.get_container_host_ip()
    port = int(ch_container.get_exposed_port(8123))
    user = ch_container.username  # type: ignore[attr-defined]
    password = ch_container.password  # type: ignore[attr-defined]

    monkeypatch.setenv("CLICKHOUSE_HOST", host)
    monkeypatch.setenv("CLICKHOUSE_PORT", str(port))
    monkeypatch.setenv("CLICKHOUSE_USER", user)
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", password)
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "false")
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", user)
    monkeypatch.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")
    monkeypatch.delenv("CLICKHOUSE_CA_CERT_PATH", raising=False)

    return ClickHouseSettings.from_env()


@pytest.fixture
def ch_client(ch_settings):
    client = build_client(ch_settings)
    try:
        ensure_service_admin(client, ch_settings)
        yield client
    finally:
        client.close()


@pytest.fixture
def prefix() -> str:
    """Per-test UUID-derived prefix for entity names. Use it for usernames,
    roles, databases, and tables so tests don't collide on shared state."""
    return "t_" + uuid.uuid4().hex[:8]
