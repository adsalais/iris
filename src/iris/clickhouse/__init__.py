"""ClickHouse provisioning, audit helpers, and FastAPI bridge.

Public surface — see ``CLAUDE.md`` for usage. The package's plain-data
helpers (handle.py, audit.py, grants.py, policies.py, users.py) are
independent of ``iris.auth``; only ``deps.py`` and ``install.py`` import
from auth, providing the FastAPI bridge.
"""

from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.bootstrap import ensure_service_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.deps import (
    CLICKHOUSE_ADMIN_ROLE,
    get_clickhouse_handle,
    require_clickhouse_admin,
)
from iris.clickhouse.grants import (
    grant_insert_update_to_table,
    grant_select_to_database,
)
from iris.clickhouse.handle import ClickHouseAdminHandle, ClickHouseHandle
from iris.clickhouse.install import install
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.users import init_user_rights

__all__ = [
    "CLICKHOUSE_ADMIN_ROLE",
    "ClickHouseAdminHandle",
    "ClickHouseHandle",
    "ClickHouseSettings",
    "add_row_policy",
    "build_client",
    "ensure_service_admin",
    "get_clickhouse_handle",
    "grant_insert_update_to_table",
    "grant_select_to_database",
    "init_user_rights",
    "install",
    "require_clickhouse_admin",
    "revoke_row_policy",
    "role_grants",
    "role_row_policies",
    "table_row_policies",
    "user_grants",
    "user_role_memberships",
    "user_row_policies",
]
