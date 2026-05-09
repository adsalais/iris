"""Row-policy CRUD helpers."""

from __future__ import annotations

import re
from typing import cast

from clickhouse_connect.driver.client import Client

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import (
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)


_FIXED_STRING_RE = re.compile(r"^FixedString\(\d+\)$")


def add_row_policy(
    client: Client,
    *,
    database: str,
    table: str,
    column: str,
    role: str,
    value: str,
) -> None:
    """Create a restrictive row policy for ``<role>`` on ``<database>.<table>``.

    The USING clause depends on the column's CH type:

    - Scalar columns (``String`` etc.): ``<column> = <value>``.
    - ``Array(String)`` and the ``Nullable`` / ``FixedString(N)`` variants:
      ``has(<column>, <value>)`` so a row matches when ``<value>`` is
      contained in the array.

    Other Array element types (``Array(Int32)``, ``Array(DateTime)``, etc.)
    raise ``TypeError`` — extend ``_build_policy_filter`` if you need them.
    A column that doesn't exist on ``<database>.<table>`` raises
    ``ValueError``.

    Also ensures two ``USING 1`` wildcard policies exist on the same table:

    - One for ``iris_global_admin`` (every global admin sees all rows).
    - One for ``<database>_DBADMIN`` (every per-database admin sees all rows).

    Names of the wildcard policies are deterministic so re-runs are idempotent
    via ``CREATE ROW POLICY IF NOT EXISTS``. The wildcards persist after the
    last restrictive policy is revoked — this matches the prior service-admin
    wildcard behavior.

    Note: ``FixedString(N)`` values must be right-padded to N bytes by the
    caller (CH stores them that way and ``has`` does not auto-pad).
    """
    validate_identifier(database, kind="database")
    validate_identifier(table, kind="table")
    validate_identifier(column, kind="column")
    validate_identifier(role, kind="role")

    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    column_q = quote_identifier(column, kind="column")
    role_q = quote_identifier(role, kind="role")

    # 1. The restrictive policy the caller asked for. Inspect the column's
    # CH type so the USING clause is correct for both scalar and Array
    # columns.
    col_type = _column_type(
        client, database=database, table=table, column=column
    )
    clause = _build_policy_filter(column_q, col_type, value)
    name = policy_name(database, table, role, value)
    name_q = quote_identifier(name, kind="policy")
    client.command(
        " ".join((
            f"CREATE ROW POLICY IF NOT EXISTS {name_q} ON {db_q}.{table_q}",
            f"FOR SELECT USING {clause} TO {role_q}",
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


def _column_type(
    client: Client, *, database: str, table: str, column: str
) -> str:
    """Return the CH type string of ``<database>.<table>.<column>``.

    Reads ``system.columns``; raises ``ValueError`` if the column does
    not exist on that table. Used by ``add_row_policy`` to decide
    between ``<col> = <val>`` and ``has(<col>, <val>)``.
    """
    rows = client.query(
        "SELECT type FROM system.columns WHERE database = {d:String} AND table = {t:String} AND name = {c:String}",
        parameters={"d": database, "t": table, "c": column},
    ).result_rows
    if not rows:
        raise ValueError(
            f"column {database}.{table}.{column} does not exist"
        )
    return cast(str, rows[0][0])


def _build_policy_filter(
    col_q: str, col_type: str, value: str
) -> str:
    """Build the row-policy USING clause for ``col_q`` of CH type ``col_type``.

    For scalar columns: ``<col_q> = <quoted value>``.
    For ``Array(String)`` and the ``Nullable`` / ``FixedString(N)``
    variants: ``has(<col_q>, <quoted value>)``.

    Raises ``TypeError`` for Array element types other than String /
    Nullable(String) / FixedString(N) / Nullable(FixedString(N)).

    ``col_q`` is the already-backtick-quoted identifier (validated by
    ``add_row_policy``'s caller path); ``value`` is quoted into a SQL
    string literal here via ``quote_string`` (regardless of branch,
    since both branches need a quoted literal).
    """
    if col_type.startswith("Array(") and col_type.endswith(")"):
        inner = col_type[len("Array(") : -1].strip()
        if inner.startswith("Nullable(") and inner.endswith(")"):
            inner = inner[len("Nullable(") : -1].strip()
        if inner != "String" and not _FIXED_STRING_RE.match(inner):
            raise TypeError(
                f"add_row_policy supports Array(String) variants only; got {col_type}. Extend add_row_policy or pass non-array columns directly."
            )
        return f"has({col_q}, {quote_string(value)})"
    return f"{col_q} = {quote_string(value)}"
