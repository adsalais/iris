"""Provisioning of per-user ClickHouse identities and group-derived role memberships."""

from __future__ import annotations

from typing import cast

from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.identifiers import quote_identifier, validate_identifier

USER_ROLE_SUFFIX = "_USER"
GROUP_ROLE_SUFFIX = "_GRP"


def provision_user(
    client: Client,
    *,
    username: str,
    groups: list[str],
    settings: ClickHouseSettings,
) -> None:
    """Idempotently provision a CH user, their per-user role, group memberships, and the
    IMPERSONATE grant for the service admin.

    The per-user role (``<username>_USER``) is granted unconditionally and is *not*
    part of the group reconcile — it represents the user's own identity, distinct
    from group membership.
    """
    validate_identifier(username, kind="username")
    for group in groups:
        validate_identifier(group, kind="group")

    user_q = quote_identifier(username, kind="username")
    user_role_q = quote_identifier(username + USER_ROLE_SUFFIX, kind="role")

    client.command(f"CREATE USER IF NOT EXISTS {user_q} IDENTIFIED WITH no_password")
    client.command(f"CREATE ROLE IF NOT EXISTS {user_role_q}")
    client.command(f"GRANT {user_role_q} TO {user_q}")

    desired_grp = {g + GROUP_ROLE_SUFFIX for g in groups}
    result = client.query(
        "SELECT granted_role_name FROM system.role_grants WHERE user_name = {u:String}",
        parameters={"u": username},
    )
    current_grp: set[str] = set()
    for row in result.named_results():
        name = cast(str, row["granted_role_name"])
        if name.endswith(GROUP_ROLE_SUFFIX):
            current_grp.add(name)

    for role in current_grp - desired_grp:
        role_q = quote_identifier(role, kind="role")
        client.command(f"REVOKE {role_q} FROM {user_q}")

    for role in desired_grp - current_grp:
        role_q = quote_identifier(role, kind="role")
        client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
        client.command(f"GRANT {role_q} TO {user_q}")

    # The IMPERSONATE grantee is the CH user iris connects as
    # (settings.user). All HTTP queries-as-user route through this identity.
    impersonator_q = quote_identifier(settings.user, kind="user")
    client.command(f"GRANT IMPERSONATE ON {user_q} TO {impersonator_q}")
