"""Provisioning of per-user ClickHouse identities and group-derived role memberships."""

from __future__ import annotations

from clickhouse_connect.driver.client import Client

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.identifiers import quote_identifier, validate_identifier

USER_ROLE_SUFFIX = "_USER"
GROUP_ROLE_SUFFIX = "_GRP"


def init_user_rights(
    client: Client,
    *,
    username: str,
    groups: list[str],
    settings: ClickHouseSettings,  # pyright: ignore[reportUnusedParameter]  — consumed in Tasks 12/13
) -> None:
    """Idempotently provision a CH user, their per-user role, group memberships, and the
    IMPERSONATE grant for the service admin.

    Steps 1–3 only in this task; group reconcile lands in Task 12, IMPERSONATE in Task 13.
    """
    validate_identifier(username, kind="username")
    for group in groups:
        validate_identifier(group, kind="group")

    user_q = quote_identifier(username, kind="username")
    user_role_q = quote_identifier(username + USER_ROLE_SUFFIX, kind="role")

    client.command(f"CREATE USER IF NOT EXISTS {user_q} IDENTIFIED WITH no_password")  # pyright: ignore[reportUnknownMemberType]
    client.command(f"CREATE ROLE IF NOT EXISTS {user_role_q}")  # pyright: ignore[reportUnknownMemberType]
    client.command(f"GRANT {user_role_q} TO {user_q}")  # pyright: ignore[reportUnknownMemberType]
