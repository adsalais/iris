"""ClickHouse-side bootstrap.

At iris launch, ``bootstrap_admin`` creates the ``iris_global_admin`` sentinel
role and (optionally) bootstraps an admin user role + admin group role from
``CLICKHOUSE_ADMIN_USER`` / ``CLICKHOUSE_ADMIN_GROUP`` env vars. Each admin role
is granted full admin privileges plus ``iris_global_admin`` (so wildcard row
policies on ``iris_global_admin`` apply to every admin's effective role set).

Detection is deterministic and per-configured-name: re-running with the
same admin name is a no-op; re-running with a *different* admin name
bootstraps the new value alongside any existing admins. The old heuristic
("any role with the matching suffix already holds ROLE ADMIN WGO") was
vulnerable to false-positives from manual operator grants on unrelated
roles.
"""

from __future__ import annotations

import logging
from typing import cast

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import DatabaseError

from iris.clickhouse.identifiers import quote_identifier
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

logger = logging.getLogger("iris.clickhouse.bootstrap")

GLOBAL_ADMIN_ROLE = "iris_global_admin"


def _admin_already_bootstrapped(client: Client, *, expected_role: str) -> bool:
    """Return True iff ``expected_role`` already has ``iris_global_admin`` granted.

    This is the deterministic alternative to the previous heuristic
    (which scanned for *any* role with the configured suffix that held
    ROLE ADMIN — vulnerable to false-positives from manual operator
    grants on unrelated roles). Re-runs with a different
    ``CLICKHOUSE_ADMIN_USER`` value DO bootstrap the new value (per-name,
    not per-channel).
    """
    rows = client.query(
        """
        SELECT count() FROM system.role_grants
        WHERE role_name = {role:String}
          AND granted_role_name = {ga:String}
        """,
        parameters={"role": expected_role, "ga": GLOBAL_ADMIN_ROLE},
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

    if admin_user:
        expected = f"{admin_user}{USER_ROLE_SUFFIX}"
        if not _admin_already_bootstrapped(client, expected_role=expected):
            role_q = quote_identifier(expected, kind="role")
            client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
            _grant_full_admin(client, role_q=role_q)
            client.command(f"GRANT {global_admin_q} TO {role_q}")
            logger.info("bootstrap: seeded admin role for user=%s", admin_user)

    if admin_group:
        expected = f"{admin_group}{GROUP_ROLE_SUFFIX}"
        if not _admin_already_bootstrapped(client, expected_role=expected):
            role_q = quote_identifier(expected, kind="role")
            client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
            _grant_full_admin(client, role_q=role_q)
            client.command(f"GRANT {global_admin_q} TO {role_q}")
            logger.info("bootstrap: seeded admin role for group=%s", admin_group)
