"""ClickHouse provisioning, audit helpers, and FastAPI bridge.

Public surface — see ``CLAUDE.md`` for usage. The package's plain-data
helpers (handle.py, audit.py, grants.py, policies.py, users.py) are
independent of ``iris.auth``; only ``deps.py``, ``install.py``, and
``rights.py`` import from auth, providing the FastAPI bridge.
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
    get_clickhouse_handle,
    require_clickhouse_admin,
    require_clickhouse_database_admin,
    require_clickhouse_database_creator,
)
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    drop_tier_roles,
    grant_insert_update_to_table,
    grant_select_to_database,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
    tier_role_name,
)
from iris.clickhouse.handle import (
    ClickHouseAdminHandle,
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
    ClickHouseHandle,
)
from iris.clickhouse.install import install
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

__all__ = [
    "ClickHouseAdminHandle",
    "ClickHouseDatabaseAdminHandle",
    "ClickHouseDatabaseCreatorHandle",
    "ClickHouseHandle",
    "ClickHouseSettings",
    "TIER_DBADMIN",
    "TIER_DBREADER",
    "TIER_DBWRITER",
    "add_row_policy",
    "build_client",
    "create_tier_roles",
    "derive_rights",
    "drop_tier_roles",
    "ensure_service_admin",
    "get_clickhouse_handle",
    "grant_insert_update_to_table",
    "grant_select_to_database",
    "grant_tier_to_group",
    "grant_tier_to_user",
    "init_user_rights",
    "install",
    "require_clickhouse_admin",
    "require_clickhouse_database_admin",
    "require_clickhouse_database_creator",
    "revoke_row_policy",
    "revoke_tier_from_group",
    "revoke_tier_from_user",
    "role_grants",
    "role_row_policies",
    "table_row_policies",
    "tier_role_name",
    "user_grants",
    "user_role_memberships",
    "user_row_policies",
]
