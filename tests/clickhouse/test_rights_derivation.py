from iris.auth.session import EMPTY_RIGHTS
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    grant_tier_to_group,
    grant_tier_to_user,
)
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights


def test_user_with_no_grants_has_empty_rights(ch_client, ch_settings, prefix):
    user = f"{prefix}_no_grants"
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    r = derive_rights(ch_client, username=user, groups=[])
    assert r == EMPTY_RIGHTS


def test_direct_user_grant_produces_reader_label(ch_client, ch_settings, prefix):
    user = f"{prefix}_reader"
    db = f"{prefix}_finance"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.db_reader == frozenset({db})
    assert r.db_writer == frozenset()
    assert r.db_admin == frozenset()


def test_group_grant_propagates_to_user(ch_client, ch_settings, prefix):
    user = f"{prefix}_via_group"
    group = f"{prefix}_engineering"
    db = f"{prefix}_logs"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    init_user_rights(ch_client, username=user, groups=[group], settings=ch_settings)
    grant_tier_to_group(ch_client, database=db, tier=TIER_DBWRITER, group=group)
    r = derive_rights(ch_client, username=user, groups=[group])
    assert r.db_writer == frozenset({db})


def test_admin_grant_yields_is_admin(ch_client, ch_settings, prefix):
    user = f"{prefix}_admin"
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    user_role = f"{user}_USER"
    # bootstrap_admin's production path is `GRANT ALL ON *.* WITH GRANT OPTION`.
    # CURRENT GRANTS is equivalent here — both produce admin-level coverage
    # in system.grants. With iris_svc holding the full privilege set
    # (granted via the conftest's users.d overlay), CH stores the result
    # as a condensed `access_type='ALL'` row rather than expanding to
    # individual privileges. derive_rights handles either form.
    ch_client.command(
        f"GRANT CURRENT GRANTS ON *.* TO `{user_role}` WITH GRANT OPTION"
    )
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.is_admin is True


def test_create_database_grant_yields_can_create(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator"
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    user_role = f"{user}_USER"
    ch_client.command(f"GRANT CREATE DATABASE ON *.* TO `{user_role}`")
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.can_create_database is True
    assert r.is_admin is False


def test_db_admin_label_set_when_grant_option_present(ch_client, ch_settings, prefix):
    user = f"{prefix}_dbadmin"
    db = f"{prefix}_owned"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    init_user_rights(ch_client, username=user, groups=[], settings=ch_settings)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBADMIN, username=user)
    r = derive_rights(ch_client, username=user, groups=[])
    assert r.db_admin == frozenset({db})
