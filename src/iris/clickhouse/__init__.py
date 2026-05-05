"""ClickHouse provisioning and audit helpers.

Public surface — see ``CLAUDE.md`` for usage. The package is independent of
``iris.auth``: it takes plain-data inputs (usernames as strings, group names as
lists) and is invoked by future code that bridges auth → clickhouse.
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
    grant_insert_update_to_table,
    grant_select_to_database,
)
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.users import init_user_rights

__all__ = [
    "ClickHouseSettings",
    "build_client",
    "ensure_service_admin",
    "init_user_rights",
    "grant_select_to_database",
    "grant_insert_update_to_table",
    "add_row_policy",
    "revoke_row_policy",
    "user_grants",
    "role_grants",
    "user_role_memberships",
    "user_row_policies",
    "role_row_policies",
    "table_row_policies",
]
