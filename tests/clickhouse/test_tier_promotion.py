"""End-to-end tier promotion: creator creates DB, grants writer to bob,
bob's session shows db_writer (but not db_admin), bob's _USER role gets
the DBWRITER role granted in CH.

This is the integration story for the new authz model — exercises the
spec's "right semantics" table top-to-bottom against a real ClickHouse.
"""
import asyncio

import httpx

from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
)
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights


def test_creator_grants_writer_promotes_target(ch_client, ch_settings, prefix):
    creator = f"{prefix}_creator"
    bob = f"{prefix}_bob"
    db = f"{prefix}_promo"

    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    asyncio.run(creator_h.create_database(db))

    init_user_rights(ch_client, username=bob, groups=[], settings=ch_settings)
    bob_rights_before = derive_rights(ch_client, username=bob, groups=[])
    assert db not in bob_rights_before.db_writer
    assert db not in bob_rights_before.db_reader

    http_stub = httpx.AsyncClient(
        base_url="http://stub",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )
    admin_h = ClickHouseDatabaseAdminHandle(
        client=ch_client,
        http_client=http_stub,
        settings=ch_settings,
        database=db,
        username=creator,
    )
    asyncio.run(admin_h.grant_writer(bob))

    bob_rights_after = derive_rights(ch_client, username=bob, groups=[])
    # Writer label set, admin not — bob can write but cannot delegate.
    assert db in bob_rights_after.db_writer
    assert db not in bob_rights_after.db_admin
    # Implied checks via the Rights helpers.
    assert bob_rights_after.has_read(db)
    assert bob_rights_after.has_write(db)
    assert not bob_rights_after.has_admin(db)


def test_creator_is_immediately_db_admin(ch_client, ch_settings, prefix):
    creator = f"{prefix}_solo_creator"
    db = f"{prefix}_solo"
    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    asyncio.run(creator_h.create_database(db))
    # The creator's _USER role was granted DBADMIN by create_database. Adding
    # init_user_rights makes the user role queryable by derive_rights's
    # transitive walk (the role exists either way; this just creates the CH
    # user that owns it, mirroring the production post-login flow).
    init_user_rights(ch_client, username=creator, groups=[], settings=ch_settings)
    rights = derive_rights(ch_client, username=creator, groups=[])
    assert db in rights.db_admin
    assert rights.has_admin(db)
    assert rights.has_write(db)
    assert rights.has_read(db)
