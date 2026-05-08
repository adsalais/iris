"""Derive a session's effective Rights from ClickHouse RBAC at login.

Walks ``system.role_grants`` transitively for the user's effective role set
(``<username>_USER`` plus ``<group>_GRP`` for each group), then queries
``system.grants`` for the global flags. Returns a frozen ``Rights`` value.

Called by the post-login hook in ``iris.clickhouse.install`` exactly once per
real login. Operator changes to grants take effect on the user's next login.
"""
from __future__ import annotations

from typing import cast

from clickhouse_connect.driver.client import Client

from iris.auth.session import Rights
from iris.clickhouse.grants import TIER_DBADMIN, TIER_DBREADER, TIER_DBWRITER
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

_TIER_SUFFIXES = (
    ("_" + TIER_DBADMIN, TIER_DBADMIN),
    ("_" + TIER_DBWRITER, TIER_DBWRITER),
    ("_" + TIER_DBREADER, TIER_DBREADER),
)


def _effective_role_set(
    client: Client, *, username: str, groups: list[str]
) -> set[str]:
    """Compute the transitive closure of role grants reachable from the user's
    seed roles (``<username>_USER`` plus each ``<group>_GRP``).

    ``system.role_grants.role_name`` is the parent that holds the grant;
    ``granted_role_name`` is the child role granted. The walk follows the
    parent→child edges starting from the seed set.
    """
    seed = {f"{username}{USER_ROLE_SUFFIX}"} | {
        f"{g}{GROUP_ROLE_SUFFIX}" for g in groups
    }
    closed: set[str] = set()
    frontier = set(seed)
    while frontier:
        closed |= frontier
        rows = client.query(
            "SELECT granted_role_name FROM system.role_grants "
            "WHERE role_name IN ({names:Array(String)})",
            parameters={"names": list(frontier)},
        ).result_rows
        next_frontier = {cast(str, r[0]) for r in rows} - closed
        frontier = next_frontier
    return closed


def derive_rights(
    client: Client, *, username: str, groups: list[str]
) -> Rights:
    """Compute the user's ``Rights`` view from CH state.

    Pre-conditions: the user's per-user role and per-group roles must already
    exist in CH. Call after ``init_user_rights``.
    """
    effective = _effective_role_set(client, username=username, groups=groups)

    db_admin: set[str] = set()
    db_writer: set[str] = set()
    db_reader: set[str] = set()
    for role in effective:
        for suffix, tier in _TIER_SUFFIXES:
            if role.endswith(suffix):
                database = role[: -len(suffix)]
                if not database:
                    continue
                if tier == TIER_DBADMIN:
                    db_admin.add(database)
                elif tier == TIER_DBWRITER:
                    db_writer.add(database)
                else:
                    db_reader.add(database)
                break

    is_admin = False
    can_create_database = False
    if effective:
        # Two separate checks against system.grants. CH stores grants in
        # expanded form — `access_type='ALL'` never appears, even when the
        # operator wrote `GRANT ALL`. Global scope is `database IS NULL`
        # (CH uses NULL, not '', for "no scope").
        #
        # is_admin marker: ROLE ADMIN at global scope with grant_option=1.
        # ROLE ADMIN is one of the privileges expanded from ALL and is only
        # granted to genuine admins; operators don't grant ROLE ADMIN
        # selectively. WGO is required because the spec defines admin as
        # having delegation power.
        #
        # can_create_database marker: CREATE DATABASE at global scope. Per
        # spec this does not require WGO.
        rows = client.query(
            "SELECT DISTINCT access_type, grant_option "
            "FROM system.grants "
            "WHERE role_name IN ({names:Array(String)}) "
            "  AND database IS NULL "
            "  AND access_type IN ('ROLE ADMIN', 'CREATE DATABASE')",
            parameters={"names": list(effective)},
        ).result_rows
        for access_type, grant_option in rows:
            access_type = cast(str, access_type)
            grant_option_v = cast(int, grant_option)
            if access_type == "ROLE ADMIN" and grant_option_v == 1:
                is_admin = True
            elif access_type == "CREATE DATABASE":
                can_create_database = True

    return Rights(
        is_admin=is_admin,
        can_create_database=can_create_database,
        db_admin=frozenset(db_admin),
        db_writer=frozenset(db_writer),
        db_reader=frozenset(db_reader),
    )
