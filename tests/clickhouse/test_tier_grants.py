from iris.clickhouse.grants import (
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
)
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX


def _granted_role_names(ch_client, *, role_name):
    rows = ch_client.query(
        "SELECT granted_role_name FROM system.role_grants WHERE role_name = {r:String}",
        parameters={"r": role_name},
    ).result_rows
    return {r[0] for r in rows}


def test_grant_tier_to_user_pre_creates_user_role(ch_client, prefix):
    db = f"{prefix}_finance"
    user = f"{prefix}_alice"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    assert f"{db}_DBREADER" in _granted_role_names(
        ch_client, role_name=f"{user}{USER_ROLE_SUFFIX}"
    )


def test_grant_tier_to_group_pre_creates_group_role(ch_client, prefix):
    db = f"{prefix}_finance"
    group = f"{prefix}_engineering"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_group(ch_client, database=db, tier=TIER_DBWRITER, group=group)
    assert f"{db}_DBWRITER" in _granted_role_names(
        ch_client, role_name=f"{group}{GROUP_ROLE_SUFFIX}"
    )


def test_grant_tier_idempotent(ch_client, prefix):
    db = f"{prefix}_idemp"
    user = f"{prefix}_bob"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)


def test_revoke_tier_removes_grant(ch_client, prefix):
    db = f"{prefix}_revoke"
    user = f"{prefix}_carol"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    revoke_tier_from_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    assert f"{db}_DBREADER" not in _granted_role_names(
        ch_client, role_name=f"{user}{USER_ROLE_SUFFIX}"
    )


def test_revoke_tier_idempotent_when_not_granted(ch_client, prefix):
    db = f"{prefix}_revoke_idemp"
    user = f"{prefix}_dave"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    revoke_tier_from_user(ch_client, database=db, tier=TIER_DBREADER, username=user)


def test_revoke_tier_from_group_idempotent(ch_client, prefix):
    db = f"{prefix}_revoke_group_idemp"
    group = f"{prefix}_eng"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    grant_tier_to_group(ch_client, database=db, tier=TIER_DBWRITER, group=group)
    revoke_tier_from_group(ch_client, database=db, tier=TIER_DBWRITER, group=group)
    assert f"{db}_DBWRITER" not in _granted_role_names(
        ch_client, role_name=f"{group}{GROUP_ROLE_SUFFIX}"
    )
