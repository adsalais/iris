"""SQL grant operations on databases and tables."""

from __future__ import annotations

from typing import Final, cast

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import DatabaseError

from iris.clickhouse.identifiers import quote_identifier, validate_identifier
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

TIER_DBADMIN: Final = "DBADMIN"
TIER_DBWRITER: Final = "DBWRITER"
TIER_DBREADER: Final = "DBREADER"

_TIERS: Final = (TIER_DBADMIN, TIER_DBWRITER, TIER_DBREADER)

# CH server error code for "role does not exist". 511 is what
# `system.errors` reports for UNKNOWN_ROLE today. We accept the symbolic
# name as a secondary signal because clickhouse-connect surfaces it in
# the error body, and matching on either side guards against either CH
# renumbering or re-wording in a single future release.
_UNKNOWN_ROLE_CODE_TOKEN: Final = "code: 511"
_UNKNOWN_ROLE_SYMBOL: Final = "UNKNOWN_ROLE"


def _is_unknown_role_error(err: DatabaseError) -> bool:
    text = str(err)
    return _UNKNOWN_ROLE_CODE_TOKEN in text.lower() or _UNKNOWN_ROLE_SYMBOL in text


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
    # Validate at the public boundary so the synthesized `<username>_USER`
    # role cannot escape the regex check via composition. `kind="role"`
    # downstream of composition skips the suffix check by design.
    validate_identifier(username, kind="username")
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
    validate_identifier(group, kind="group")
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
    role; we swallow the "not found" case (matched on CH error code 523)
    and let any other error propagate.
    """
    validate_identifier(username, kind="username")
    user_role = f"{username}{USER_ROLE_SUFFIX}"
    user_role_q = quote_identifier(user_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    try:
        client.command(f"REVOKE {tier_q} FROM {user_role_q}")
    except DatabaseError as err:
        if _is_unknown_role_error(err):
            return
        raise


def revoke_tier_from_group(
    client: Client, *, database: str, tier: str, group: str
) -> None:
    """``REVOKE <database>_<tier> FROM <group>_GRP``.

    Does NOT pre-create the group-role; CH may raise on a missing role
    (we swallow code 523 only).
    """
    validate_identifier(group, kind="group")
    group_role = f"{group}{GROUP_ROLE_SUFFIX}"
    group_role_q = quote_identifier(group_role, kind="role")
    tier_q = quote_identifier(tier_role_name(database, tier), kind="role")
    try:
        client.command(f"REVOKE {tier_q} FROM {group_role_q}")
    except DatabaseError as err:
        if _is_unknown_role_error(err):
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


def list_tier_members(
    client: Client, *, database: str,
) -> dict[str, list[dict[str, str]]]:
    """Return tier-role members for ``database``, keyed by tier.

    Result shape: ``{"admin": [...], "reader": [...], "writer": [...]}``.
    Each entry is ``{"kind": "user" | "role", "name": <str>}`` — derived from
    ``system.role_grants`` rows that target the per-database tier role
    (``<database>_DBADMIN``, ``<database>_DBWRITER``, ``<database>_DBREADER``).
    """
    out: dict[str, list[dict[str, str]]] = {"admin": [], "reader": [], "writer": []}
    for tier_const, tier_key in (
        (TIER_DBADMIN, "admin"),
        (TIER_DBREADER, "reader"),
        (TIER_DBWRITER, "writer"),
    ):
        role = tier_role_name(database, tier_const)
        rows = client.query(
            "SELECT user_name, role_name FROM system.role_grants "
            + "WHERE granted_role_name = {r:String}",
            parameters={"r": role},
        )
        for row in rows.named_results():
            u = row.get("user_name")
            r2 = row.get("role_name")
            if u:
                out[tier_key].append({"kind": "user", "name": cast(str, u)})
            elif r2:
                out[tier_key].append({"kind": "role", "name": cast(str, r2)})
    return out
