"""ClickHouse provisioning, audit helpers, and per-tier ops.

Public surface — see ``CLAUDE.md`` for usage. Session subclasses in
``iris.auth.identity`` call into these helpers via ``asyncio.to_thread``.

The ``install`` function lives in ``iris.clickhouse.install`` but is *not*
re-exported from this package: callers (only ``iris.app:build_app``) do
``from iris.clickhouse.install import install``. Removing it from this
``__init__`` breaks an old module-load cycle where importing the package
triggered loading ``iris.auth.bootstrap`` via ``install``.
"""
from __future__ import annotations

from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE, bootstrap_admin
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
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

__all__ = [
    "ClickHouseSettings",
    "GLOBAL_ADMIN_ROLE",
    "TIER_DBADMIN",
    "TIER_DBREADER",
    "TIER_DBWRITER",
    "add_row_policy",
    "bootstrap_admin",
    "build_client",
    "create_tier_roles",
    "derive_rights",
    "drop_tier_roles",
    "grant_insert_update_to_table",
    "grant_select_to_database",
    "grant_tier_to_group",
    "grant_tier_to_user",
    "init_user_rights",
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
