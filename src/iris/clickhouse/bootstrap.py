"""ClickHouse-side bootstrap.

Two responsibilities, both idempotent:

- ``ensure_service_admin``: creates the configured CH role and grants it to the
  configured user, so iris's connection identity has the privileges it needs to
  manage RBAC. (Deprecated — to be removed once callers stop referencing it.)

- ``bootstrap_admin``: at iris launch, creates the ``iris_global_admin`` sentinel
  role and (optionally) bootstraps an admin user role + admin group role from
  ``CLICKHOUSE_ADMIN_USER`` / ``CLICKHOUSE_ADMIN_GROUP`` env vars. Each admin role
  is granted full admin privileges plus ``iris_global_admin`` (so wildcard row
  policies on ``iris_global_admin`` apply to every admin's effective role set).
"""

from __future__ import annotations

import logging
from typing import cast

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import DatabaseError

from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.identifiers import quote_identifier
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

logger = logging.getLogger("iris.clickhouse.bootstrap")

GLOBAL_ADMIN_ROLE = "iris_global_admin"


def ensure_service_admin(client: Client, settings: ClickHouseSettings) -> None:
    """DEPRECATED — kept for the breakage window only.

    The ``service_admin_role`` concept goes away in this refactor. This
    function becomes dead code once ``ClickHouseSettings`` drops
    ``service_admin_user`` / ``service_admin_role``. Don't extend it.
    """
    role = quote_identifier(settings.service_admin_role, kind="service_admin_role")
    user = quote_identifier(settings.service_admin_user, kind="service_admin_user")
    client.command(f"CREATE ROLE IF NOT EXISTS {role}")
    client.command(f"GRANT {role} TO {user}")


def _has_admin_role_with_suffix(client: Client, suffix: str) -> bool:
    """Detect whether some role with the given suffix already holds the admin
    marker (ROLE ADMIN at global scope with grant_option=1)."""
    rows = client.query(
        """
        SELECT count() FROM system.grants
        WHERE access_type = 'ROLE ADMIN'
          AND grant_option = 1
          AND database IS NULL
          AND endsWith(role_name, {suffix:String})
        """,
        parameters={"suffix": suffix},
    ).result_rows
    return cast(int, rows[0][0]) > 0


def _grant_full_admin(client: Client, *, role_q: str) -> None:
    """``GRANT ALL ON *.* WITH GRANT OPTION``, with a ``CURRENT GRANTS`` fallback
    for the testcontainer's missing NAMED COLLECTION ADMIN privilege."""
    try:
        client.command(f"GRANT ALL ON *.* TO {role_q} WITH GRANT OPTION")
    except DatabaseError as err:
        if "NAMED COLLECTION ADMIN" not in str(err):
            raise
        client.command(
            f"GRANT CURRENT GRANTS ON *.* TO {role_q} WITH GRANT OPTION"
        )


def bootstrap_admin(
    client: Client,
    *,
    admin_user: str | None = None,
    admin_group: str | None = None,
) -> None:
    """Bootstrap iris's admin tier on ClickHouse. Idempotent across both channels.

    Always creates the ``iris_global_admin`` sentinel role (no privileges of
    its own — wildcard row policies attach to it). When ``admin_user`` is
    supplied AND no role with the ``_USER`` suffix already holds admin,
    creates ``<admin_user>_USER`` with full admin grants and
    ``iris_global_admin`` granted to it. When ``admin_group`` is supplied,
    the same for ``<admin_group>_GRP``.

    Both channels are independently idempotent: re-running with an existing
    admin in the channel is a no-op. Wiping CH and restarting re-triggers
    both.
    """
    global_admin_q = quote_identifier(GLOBAL_ADMIN_ROLE, kind="role")
    client.command(f"CREATE ROLE IF NOT EXISTS {global_admin_q}")

    if admin_user and not _has_admin_role_with_suffix(client, USER_ROLE_SUFFIX):
        role = f"{admin_user}{USER_ROLE_SUFFIX}"
        role_q = quote_identifier(role, kind="role")
        client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
        _grant_full_admin(client, role_q=role_q)
        client.command(f"GRANT {global_admin_q} TO {role_q}")
        logger.info("bootstrap: seeded admin role for user=%s", admin_user)

    if admin_group and not _has_admin_role_with_suffix(client, GROUP_ROLE_SUFFIX):
        role = f"{admin_group}{GROUP_ROLE_SUFFIX}"
        role_q = quote_identifier(role, kind="role")
        client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
        _grant_full_admin(client, role_q=role_q)
        client.command(f"GRANT {global_admin_q} TO {role_q}")
        logger.info("bootstrap: seeded admin role for group=%s", admin_group)
