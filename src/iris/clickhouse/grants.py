"""SQL grant operations on databases and tables."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.identifiers import quote_identifier


def grant_select_to_database(client: Client, *, database: str, role: str) -> None:
    """``GRANT SELECT ON <database>.* TO <role>``. Idempotent (CH no-ops on re-grant)."""
    db_q = quote_identifier(database, kind="database")
    role_q = quote_identifier(role, kind="role")
    client.command(f"GRANT SELECT ON {db_q}.* TO {role_q}")  # pyright: ignore[reportUnknownMemberType]
