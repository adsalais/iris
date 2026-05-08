"""Bootstrap option β: seed the first ClickHouse admin user at app boot.

Runs at app boot after ``ensure_service_admin``. Idempotent: if any role already
holds the admin marker (ROLE ADMIN at global scope with grant_option=1), the
function is a no-op. Wiping the CH server and restarting iris re-triggers the
seed.

The bootstrap user need not exist in the IdP yet. iris creates the corresponding
``<username>_USER`` role in CH and grants it ``ALL ON *.* WITH GRANT OPTION``;
when the operator logs in for the first time, ``init_user_rights`` reuses the
existing role and ``derive_rights`` returns ``is_admin=True``.

The grant fall-back to ``CURRENT GRANTS`` exists for the test container's
restricted privilege envelope (the testcontainer's root user lacks
NAMED COLLECTION ADMIN, which is part of CH's ``ALL`` superset). In a real
deployment where the iris service identity has full ``ALL`` privileges, the
first GRANT succeeds and the fall-back is unused.
"""
from __future__ import annotations

import logging
from typing import cast

from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import DatabaseError

from iris.clickhouse.identifiers import quote_identifier
from iris.clickhouse.users import USER_ROLE_SUFFIX

logger = logging.getLogger("iris.auth.bootstrap")


def _admin_exists(client: Client) -> bool:
    """Detect whether some iris user role already holds the admin marker —
    ROLE ADMIN at global scope with ``grant_option=1``.

    Restricted to roles ending in ``_USER`` so the service identity (which
    necessarily holds ROLE ADMIN+WGO to manage iris's own RBAC state) is not
    mistaken for a bootstrapped admin user.
    """
    rows = client.query(
        "SELECT count() FROM system.grants "
        "WHERE access_type = 'ROLE ADMIN' "
        "  AND grant_option = 1 "
        "  AND database IS NULL "
        "  AND endsWith(role_name, {suffix:String})",
        parameters={"suffix": USER_ROLE_SUFFIX},
    ).result_rows
    return cast(int, rows[0][0]) > 0


def bootstrap_admin(client: Client, *, username: str) -> None:
    """Seed ``<username>_USER`` with admin grants when no admin exists in CH.

    No-op when an admin grant is already present. Idempotent across restarts.
    """
    if _admin_exists(client):
        logger.info("bootstrap: admin already present in CH; skipping seed")
        return
    role = f"{username}{USER_ROLE_SUFFIX}"
    role_q = quote_identifier(role, kind="role")
    client.command(f"CREATE ROLE IF NOT EXISTS {role_q}")
    try:
        client.command(f"GRANT ALL ON *.* TO {role_q} WITH GRANT OPTION")
    except DatabaseError as err:
        if "NAMED COLLECTION ADMIN" not in str(err):
            raise
        # Test container fallback — the service identity lacks NAMED
        # COLLECTION ADMIN, so GRANT ALL fails. CURRENT GRANTS delegates
        # whatever the granter actually holds, which is enough to trigger
        # the ROLE ADMIN+WGO admin marker derive_rights checks for.
        client.command(
            f"GRANT CURRENT GRANTS ON *.* TO {role_q} WITH GRANT OPTION"
        )
    logger.info("bootstrap: seeded admin role for username=%s", username)
