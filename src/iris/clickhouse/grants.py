"""SQL grant operations on databases and tables."""

from __future__ import annotations

from typing import Final

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import DatabaseError

from iris.clickhouse.identifiers import quote_identifier, validate_identifier
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

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


def _ensure_role(client: Client, role: str) -> None:
    """``CREATE ROLE IF NOT EXISTS`` — pre-creates the role so grants succeed
    even if the user/group has never authenticated. Closes username enumeration
    via differential CH errors."""
    role_q = quote_identifier(role, kind="role")
    client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")


def grant_tier_to_user(
    client: Client, *, database: str, tier: str, username: str
) -> None:
    """``GRANT <database>_<tier> TO <username>_USER``.

    Pre-creates the user role if it does not yet exist (closes a username
    enumeration channel via differential CH errors).
    """
    user_role = f"{username}{USER_ROLE_SUFFIX}"
    _ensure_role(client, user_role)
    user_role_q = quote_identifier(user_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    client.command(f"GRANT {tier_q} TO {user_role_q}")


def grant_tier_to_group(
    client: Client, *, database: str, tier: str, group: str
) -> None:
    """``GRANT <database>_<tier> TO <group>_GRP``.

    Pre-creates the group role if it does not yet exist.
    """
    group_role = f"{group}{GROUP_ROLE_SUFFIX}"
    _ensure_role(client, group_role)
    group_role_q = quote_identifier(group_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    client.command(f"GRANT {tier_q} TO {group_role_q}")


def revoke_tier_from_user(
    client: Client, *, database: str, tier: str, username: str
) -> None:
    """``REVOKE <database>_<tier> FROM <username>_USER``.

    Does NOT pre-create the user-role: revoke must not leak state for
    arbitrary attacker-supplied usernames. CH may raise on a missing
    role; we swallow the "not found" case and let any other error
    propagate.
    """
    user_role = f"{username}{USER_ROLE_SUFFIX}"
    validate_identifier(user_role, kind="role")
    user_role_q = quote_identifier(user_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    try:
        client.command(f"REVOKE {tier_q} FROM {user_role_q}")
    except DatabaseError as err:
        if "UNKNOWN_ROLE" in str(err):
            return
        raise


def revoke_tier_from_group(
    client: Client, *, database: str, tier: str, group: str
) -> None:
    """``REVOKE <database>_<tier> FROM <group>_GRP``.

    Does NOT pre-create the group-role; CH may raise on a missing role
    (we swallow "not found").
    """
    group_role = f"{group}{GROUP_ROLE_SUFFIX}"
    validate_identifier(group_role, kind="role")
    group_role_q = quote_identifier(group_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    try:
        client.command(f"REVOKE {tier_q} FROM {group_role_q}")
    except DatabaseError as err:
        if "UNKNOWN_ROLE" in str(err):
            return
        raise


def grant_select_to_database(client: Client, *, database: str, role: str) -> None:
    """``GRANT SELECT ON <database>.* TO <role>``."""
    db_q = quote_identifier(database, kind="database")
    role_q = quote_identifier(role, kind="role")
    client.command(f"GRANT SELECT ON {db_q}.* TO {role_q}")


def revoke_select_from_database(client: Client, *, database: str, role: str) -> None:
    """``REVOKE SELECT ON <database>.* FROM <role>``."""
    db_q = quote_identifier(database, kind="database")
    role_q = quote_identifier(role, kind="role")
    client.command(f"REVOKE SELECT ON {db_q}.* FROM {role_q}")


def grant_insert_update_to_table(
    client: Client, *, database: str, table: str, role: str
) -> None:
    """``GRANT INSERT`` and ``GRANT ALTER UPDATE`` on ``<database>.<table>`` to ``<role>``."""
    db_q = quote_identifier(database, kind="database")
    table_q = quote_identifier(table, kind="table")
    role_q = quote_identifier(role, kind="role")
    client.command(f"GRANT INSERT ON {db_q}.{table_q} TO {role_q}")
    client.command(f"GRANT ALTER UPDATE ON {db_q}.{table_q} TO {role_q}")
