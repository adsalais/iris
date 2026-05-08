"""Row-policy CRUD helpers."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
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
) -> None:
    """Create a row policy ``<column> = <value>`` for ``<role>`` on ``<database>.<table>``.

    Also ensures two ``USING 1`` wildcard policies exist on the same table:

    - One for ``iris_global_admin`` (every global admin sees all rows).
    - One for ``<database>_DBADMIN`` (every per-database admin sees all rows).

    Names of the wildcard policies are deterministic so re-runs are idempotent
    via ``CREATE ROW POLICY IF NOT EXISTS``. The wildcards persist after the
    last restrictive policy is revoked — this matches the prior service-admin
    wildcard behavior.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(column, kind="column")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    column_q = quote_identifier(column, kind="column")
    role_q = quote_identifier(role, kind="role")

    # 1. The restrictive policy the caller asked for.
    name = policy_name(database, table, role, value)
    name_q = quote_identifier(name, kind="policy")
    client.command(
        " ".join((
            f"CREATE ROW POLICY IF NOT EXISTS {name_q} ON {db_q}.{table_q}",
            f"FOR SELECT USING {column_q} = {quote_string(value)} TO {role_q}",
        ))
    )

    # 2. The iris_global_admin wildcard (deterministic name, idempotent).
    ga_name = f"{database}_{table}_{GLOBAL_ADMIN_ROLE}"
    ga_name_q = quote_identifier(ga_name, kind="policy")
    ga_role_q = quote_identifier(GLOBAL_ADMIN_ROLE, kind="role")
    client.command(
        " ".join((
            f"CREATE ROW POLICY IF NOT EXISTS {ga_name_q} ON {db_q}.{table_q}",
            f"FOR SELECT USING 1 TO {ga_role_q}",
        ))
    )

    # 3. The <database>_DBADMIN wildcard (deterministic name, idempotent).
    dba_role = tier_role_name(database, TIER_DBADMIN)
    dba_name = f"{database}_{table}_{dba_role}"
    dba_name_q = quote_identifier(dba_name, kind="policy")
    dba_role_q = quote_identifier(dba_role, kind="role")
    client.command(
        " ".join((
            f"CREATE ROW POLICY IF NOT EXISTS {dba_name_q} ON {db_q}.{table_q}",
            f"FOR SELECT USING 1 TO {dba_role_q}",
        ))
    )


def revoke_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    role: str,
    value: str,
) -> None:
    """Drop the named restrictive row policy created by ``add_row_policy``.

    Wildcards on ``iris_global_admin`` and ``<database>_DBADMIN`` are *not*
    dropped — they may apply to other restrictive policies on the same table,
    and persist intentionally so admins continue to see all rows.
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    name_q = quote_identifier(policy_name(database, table, role, value), kind="policy")
    client.command(f"DROP ROW POLICY IF EXISTS {name_q} ON {db_q}.{table_q}")
