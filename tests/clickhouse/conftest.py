"""Fixtures for the ClickHouse test suite.

Spins up a real ClickHouse server in a Docker container once per pytest session,
populates the CLICKHOUSE_* env vars to point at it, and yields a `build_client`
result with the service admin already bootstrapped.

Tests should namespace any entities they create (users, roles, databases, tables)
with the ``prefix`` fixture, since state accumulates across tests within a session.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import clickhouse_connect
import pytest
from testcontainers.clickhouse import ClickHouseContainer

from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings

# SQL-managed service admin credentials used throughout the session.
# Must differ from the XML-defined ``test`` user so that ``GRANT role TO user``
# can target a writable (``local_directory``) storage entry.
_SVC_USER = "iris_svc"
_SVC_PASSWORD = "iris_svc_pw"


@pytest.fixture(scope="session")
def ch_container():
    """One ClickHouse server per test session.

    ``CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`` grants the XML-defined
    ``test`` user the ``CREATE ROLE`` / ``GRANT`` DDL rights needed to
    bootstrap the service-admin role.  We then create a SQL-managed
    ``iris_svc`` user (stored in ``local_directory``, writable) and use it
    as ``service_admin_user`` so that the ``GRANT role TO user`` DDL can
    actually persist.
    """
    # Overlay that grants NAMED COLLECTION ADMIN to the `test` user. CH's
    # /etc/clickhouse-server/users.d/*.xml files are merged into the user
    # definitions at startup. Without this, `test` lacks NAMED COLLECTION
    # ADMIN and therefore cannot delegate it to iris_svc, which would
    # break iris.bootstrap_admin's GRANT ALL.
    users_d_overlay = (
        Path(__file__).parent.parent / "seed" / "users.d" / "test-named-collection-admin.xml"
    ).resolve()

    container = (
        ClickHouseContainer("clickhouse/clickhouse-server:26.3")
        .with_env("CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT", "1")
        .with_volume_mapping(
            str(users_d_overlay),
            "/etc/clickhouse-server/users.d/test-named-collection-admin.xml",
            "ro",
        )
    )
    with container as ch:
        host = ch.get_container_host_ip()
        port = int(ch.get_exposed_port(8123))
        # Connect as the privileged XML user to create the SQL-managed svc user.
        admin = clickhouse_connect.get_client(
            host=host,
            port=port,
            username=ch.username,  # type: ignore[attr-defined]
            password=ch.password,  # type: ignore[attr-defined]
            secure=False,
            verify=False,
        )
        try:
            admin.command(f"CREATE USER IF NOT EXISTS {_SVC_USER} IDENTIFIED BY '{_SVC_PASSWORD}'")
            # Give the svc user enough privilege to run the bootstrap DDL itself
            # (so that ensure_service_admin called *as* iris_svc would also work).
            admin.command(
                f"GRANT CREATE ROLE, ROLE ADMIN ON *.* TO {_SVC_USER}"
            )
            # Allow the svc user to create SQL-managed users (needed by provision_user).
            admin.command(
                f"GRANT CREATE USER ON *.* TO {_SVC_USER}"
            )
            # Allow the svc user to verify its own bootstrap state via system tables.
            admin.command(
                f"GRANT SELECT ON system.roles TO {_SVC_USER}"
            )
            admin.command(
                f"GRANT SELECT ON system.role_grants TO {_SVC_USER}"
            )
            admin.command(
                f"GRANT SELECT ON system.users TO {_SVC_USER}"
            )
            admin.command(
                f"GRANT SELECT ON system.grants TO {_SVC_USER}"
            )
            admin.command(
                f"GRANT SELECT ON system.row_policies TO {_SVC_USER}"
            )
            # Allow iris_svc to grant IMPERSONATE on provisioned users to itself.
            # "GRANT IMPERSONATE ON *.* TO <user> WITH GRANT OPTION" is supported
            # in ClickHouse 26.x (the container is pinned to "latest").  The
            # wildcard form gives iris_svc the right to later run
            # "GRANT IMPERSONATE ON <target_user> TO iris_svc" inside
            # provision_user.  Note: when a wildcard IMPERSONATE grant already
            # exists, per-user grants are silently absorbed (no extra row in
            # system.grants); tests verify coverage via the wildcard row.
            admin.command(
                f"GRANT IMPERSONATE ON *.* TO {_SVC_USER} WITH GRANT OPTION"
            )
            # Allow iris_svc to create databases (needed for grant tests).
            admin.command(
                f"GRANT CREATE DATABASE ON *.* TO {_SVC_USER}"
            )
            # Allow iris_svc to grant SELECT on databases (needed for grant_select_to_database tests).
            admin.command(
                f"GRANT SELECT ON *.* TO {_SVC_USER} WITH GRANT OPTION"
            )
            # Allow iris_svc to create tables (needed for grant tests).
            admin.command(
                f"GRANT CREATE TABLE ON *.* TO {_SVC_USER}"
            )
            # Allow iris_svc to grant INSERT and ALTER UPDATE (needed for grant_insert_update_to_table tests).
            admin.command(
                f"GRANT INSERT, ALTER UPDATE ON *.* TO {_SVC_USER} WITH GRANT OPTION"
            )
            # Allow iris_svc to create and drop row policies (needed for row policy tests).
            admin.command(
                f"GRANT CREATE ROW POLICY, DROP ROW POLICY ON *.* TO {_SVC_USER}"
            )
            # Allow iris_svc to drop roles (needed for tier-role lifecycle helpers
            # and delete_database). ROLE ADMIN by itself does not include DROP ROLE.
            admin.command(
                f"GRANT DROP ROLE ON *.* TO {_SVC_USER}"
            )
            # Allow iris_svc to grant database-scoped privileges with GRANT
            # OPTION on per-database tier roles. The `test` user (XML-defined,
            # privileged but not the absolute root) lacks server-scope rarities
            # like NAMED COLLECTION ADMIN, so `GRANT ALL ON *.*` would fail.
            # CURRENT GRANTS delegates exactly what `test` holds, which is
            # enough for the database-scope ALL we issue at tier-role creation.
            admin.command(
                f"GRANT CURRENT GRANTS ON *.* TO {_SVC_USER} WITH GRANT OPTION"
            )
        finally:
            admin.close()
        yield ch


@pytest.fixture
def ch_settings(ch_container, monkeypatch):
    """ClickHouseSettings pointing at the running container.

    Each test gets a fresh ``from_env()`` so the dataclass is hermetic — the env
    overrides are scoped to the test by ``monkeypatch``.

    ``CLICKHOUSE_USER`` / ``CLICKHOUSE_PASSWORD`` are set to the SQL-managed
    ``iris_svc`` user so that ``ensure_service_admin`` can both run
    ``CREATE ROLE`` and grant it to a writable-storage user.
    """
    host = ch_container.get_container_host_ip()
    port = int(ch_container.get_exposed_port(8123))

    monkeypatch.setenv("CLICKHOUSE_HOST", host)
    monkeypatch.setenv("CLICKHOUSE_PORT", str(port))
    monkeypatch.setenv("CLICKHOUSE_USER", _SVC_USER)
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", _SVC_PASSWORD)
    monkeypatch.setenv("CLICKHOUSE_SECURE", "false")
    monkeypatch.setenv("CLICKHOUSE_VERIFY", "false")
    monkeypatch.delenv("CLICKHOUSE_CA_CERT_PATH", raising=False)

    return ClickHouseSettings.from_env()


@pytest.fixture
def ch_client(ch_settings):
    client = build_client(ch_settings)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def prefix() -> str:
    """Per-test UUID-derived prefix for entity names. Use it for usernames,
    roles, databases, and tables so tests don't collide on shared state."""
    return "t_" + uuid.uuid4().hex[:8]
