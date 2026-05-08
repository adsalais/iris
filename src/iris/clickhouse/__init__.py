"""ClickHouse provisioning, audit helpers, and per-tier ops.

Public surface — see ``CLAUDE.md`` for usage. ``iris.clickhouse`` no longer
hosts FastAPI handle providers; the Session subclasses in
``iris.auth.identity`` carry the per-tier method surface, calling into the
``*_impl`` functions in ``iris.clickhouse.handle``.
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
from iris.clickhouse.install import install
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

__all__ = [
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
    "grant_insert_update_to_table",
    "grant_select_to_database",
    "grant_tier_to_group",
    "grant_tier_to_user",
    "init_user_rights",
    "install",
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
