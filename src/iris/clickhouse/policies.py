"""Row-policy CRUD helpers."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.identifiers import (
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)


def add_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    column: str,
    role: str,
    value: str,
    settings: ClickHouseSettings,
) -> None:
    """Create a row policy ``<column> = <value>`` for ``<role>`` on ``<database>.<table>``.

    Also ensures a wildcard ``USING 1`` policy exists for ``settings.service_admin_role``
    so the service admin can read every row regardless of other policies. The wildcard
    name is the constant ``<database>_<table>_<service_admin_role>``; subsequent calls
    are no-ops thanks to ``IF NOT EXISTS``.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(column, kind="column")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    column_q = quote_identifier(column, kind="column")
    role_q = quote_identifier(role, kind="role")

    name = policy_name(database, table, role, value)
    name_q = quote_identifier(name, kind="policy")
    client.command(  # pyright: ignore[reportUnknownMemberType]
        f"CREATE ROW POLICY IF NOT EXISTS {name_q} ON {db_q}.{table_q} FOR SELECT USING {column_q} = {quote_string(value)} TO {role_q}"
    )

    sa_role = settings.service_admin_role
    sa_role_q = quote_identifier(sa_role, kind="service_admin_role")
    sa_name = f"{database}_{table}_{sa_role}"
    sa_name_q = quote_identifier(sa_name, kind="policy")
    client.command(  # pyright: ignore[reportUnknownMemberType]
        f"CREATE ROW POLICY IF NOT EXISTS {sa_name_q} ON {db_q}.{table_q} FOR SELECT USING 1 TO {sa_role_q}"
    )


def revoke_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    role: str,
    value: str,
) -> None:
    """Drop the named row policy created by ``add_row_policy(database, table, column, role, value)``.

    The wildcard service-admin policy is *not* dropped — it's a singleton per
    ``(database, table, service_admin_role)`` triple and may still apply to other
    policies on the same table.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    name_q = quote_identifier(policy_name(database, table, role, value), kind="policy")
    client.command(f"DROP ROW POLICY IF EXISTS {name_q} ON {db_q}.{table_q}")  # pyright: ignore[reportUnknownMemberType]
