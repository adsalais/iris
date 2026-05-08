"""SQL grant operations on databases and tables."""

from __future__ import annotations

from typing import Final

from clickhouse_connect.driver.client import Client

from iris.clickhouse.identifiers import quote_identifier

TIER_DBADMIN: Final = "DBADMIN"
TIER_DBWRITER: Final = "DBWRITER"
TIER_DBREADER: Final = "DBREADER"

_TIERS: Final = (TIER_DBADMIN, TIER_DBWRITER, TIER_DBREADER)


def tier_role_name(database: str, tier: str) -> str:
    """Return the tier role name for ``database`` and tier (one of
    ``TIER_DBADMIN``, ``TIER_DBWRITER``, ``TIER_DBREADER``)."""
    if tier not in _TIERS:
        raise ValueError(f"unknown tier: {tier!r}")
    return f"{database}_{tier}"


def create_tier_roles(client: Client, *, database: str) -> None:
    """Create the three tier roles for ``database`` and grant their privileges.
    Idempotent. Caller is responsible for ``CREATE DATABASE``."""
    db_q = quote_identifier(database, kind="database")
    admin_role = tier_role_name(database, TIER_DBADMIN)
    writer_role = tier_role_name(database, TIER_DBWRITER)
    reader_role = tier_role_name(database, TIER_DBREADER)
    admin_q = quote_identifier(admin_role, kind="role")
    writer_q = quote_identifier(writer_role, kind="role")
    reader_q = quote_identifier(reader_role, kind="role")
    client.command(f"CREATE ROLE IF NOT EXISTS {admin_q}")
    client.command(f"CREATE ROLE IF NOT EXISTS {writer_q}")
    client.command(f"CREATE ROLE IF NOT EXISTS {reader_q}")
    client.command(f"GRANT ALL ON {db_q}.* TO {admin_q} WITH GRANT OPTION")
    client.command(f"GRANT SELECT, INSERT, ALTER UPDATE ON {db_q}.* TO {writer_q}")
    client.command(f"GRANT SELECT ON {db_q}.* TO {reader_q}")


def drop_tier_roles(client: Client, *, database: str) -> None:
    """Drop the three tier roles for ``database``. Idempotent."""
    admin_q = quote_identifier(tier_role_name(database, TIER_DBADMIN), kind="role")
    writer_q = quote_identifier(tier_role_name(database, TIER_DBWRITER), kind="role")
    reader_q = quote_identifier(tier_role_name(database, TIER_DBREADER), kind="role")
    client.command(f"DROP ROLE IF EXISTS {admin_q}")
    client.command(f"DROP ROLE IF EXISTS {writer_q}")
    client.command(f"DROP ROLE IF EXISTS {reader_q}")


def grant_select_to_database(client: Client, *, database: str, role: str) -> None:
    """``GRANT SELECT ON <database>.* TO <role>``. Idempotent (CH no-ops on re-grant)."""
    db_q = quote_identifier(database, kind="database")
    role_q = quote_identifier(role, kind="role")
    client.command(f"GRANT SELECT ON {db_q}.* TO {role_q}")


def revoke_select_from_database(client: Client, *, database: str, role: str) -> None:
    """``REVOKE SELECT ON <database>.* FROM <role>``. Idempotent (CH no-ops if no grant)."""
    db_q = quote_identifier(database, kind="database")
    role_q = quote_identifier(role, kind="role")
    client.command(f"REVOKE SELECT ON {db_q}.* FROM {role_q}")


def grant_insert_update_to_table(
    client: Client, *, database: str, table: str, role: str
) -> None:
    """``GRANT INSERT`` and ``GRANT ALTER UPDATE`` on ``<database>.<table>`` to ``<role>``.
    Both grants are idempotent."""
    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    role_q = quote_identifier(role, kind="role")
    client.command(f"GRANT INSERT ON {db_q}.{table_q} TO {role_q}")
    client.command(f"GRANT ALTER UPDATE ON {db_q}.{table_q} TO {role_q}")
