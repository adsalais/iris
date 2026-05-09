from iris.auth.rights import EMPTY_CAPABILITIES
from iris.clickhouse.capabilities import derive_capabilities
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    grant_tier_to_group,
    grant_tier_to_user,
)
from iris.clickhouse.users import provision_user


def test_user_with_no_grants_has_empty_capabilities(ch_client, ch_settings, prefix):
    user = f"{prefix}_no_grants"
    provision_user(ch_client, username=user, groups=[], settings=ch_settings)
    c = derive_capabilities(ch_client, username=user, groups=[])
    assert c == EMPTY_CAPABILITIES


def test_direct_user_grant_produces_reader_label(ch_client, ch_settings, prefix):
    user = f"{prefix}_reader"
    db = f"{prefix}_finance"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    provision_user(ch_client, username=user, groups=[], settings=ch_settings)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBREADER, username=user)
    c = derive_capabilities(ch_client, username=user, groups=[])
    assert c.db_reader == frozenset({db})
    assert c.db_writer == frozenset()
    assert c.db_admin == frozenset()


def test_group_grant_propagates_to_user(ch_client, ch_settings, prefix):
    user = f"{prefix}_via_group"
    group = f"{prefix}_engineering"
    db = f"{prefix}_logs"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    provision_user(ch_client, username=user, groups=[group], settings=ch_settings)
    grant_tier_to_group(ch_client, database=db, tier=TIER_DBWRITER, group=group)
    c = derive_capabilities(ch_client, username=user, groups=[group])
    assert c.db_writer == frozenset({db})


def test_admin_grant_yields_is_admin(ch_client, ch_settings, prefix):
    user = f"{prefix}_admin"
    provision_user(ch_client, username=user, groups=[], settings=ch_settings)
    user_role = f"{user}_USER"
    # bootstrap_admin's production path is `GRANT ALL ON *.* WITH GRANT OPTION`.
    # CURRENT GRANTS is equivalent here — both produce admin-level coverage
    # in system.grants. With iris_svc holding the full privilege set
    # (granted via the conftest's users.d overlay), CH stores the result
    # as a condensed `access_type='ALL'` row rather than expanding to
    # individual privileges. derive_capabilities handles either form.
    ch_client.command(
        f"GRANT CURRENT GRANTS ON *.* TO `{user_role}` WITH GRANT OPTION"
    )
    c = derive_capabilities(ch_client, username=user, groups=[])
    assert c.is_admin is True


def test_create_database_grant_yields_can_create(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator"
    provision_user(ch_client, username=user, groups=[], settings=ch_settings)
    user_role = f"{user}_USER"
    ch_client.command(f"GRANT CREATE DATABASE ON *.* TO `{user_role}`")
    c = derive_capabilities(ch_client, username=user, groups=[])
    assert c.can_create_database is True
    assert c.is_admin is False


def test_db_admin_label_set_when_grant_option_present(ch_client, ch_settings, prefix):
    user = f"{prefix}_dbadmin"
    db = f"{prefix}_owned"
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    create_tier_roles(ch_client, database=db)
    provision_user(ch_client, username=user, groups=[], settings=ch_settings)
    grant_tier_to_user(ch_client, database=db, tier=TIER_DBADMIN, username=user)
    c = derive_capabilities(ch_client, username=user, groups=[])
    assert c.db_admin == frozenset({db})
