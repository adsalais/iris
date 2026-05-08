"""Tests for ClickHouseDatabaseAdminHandle: tier grants/revokes plus
delete_database. Uses an httpx.AsyncClient transport-mocked since these
tests don't exercise query_as_user — only the admin operations that go
through clickhouse-connect."""
import asyncio

import httpx

from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
)
from iris.clickhouse.rights import derive_rights


def _admin_handle(ch_client, ch_settings, *, database: str, username: str):
    """A real-CH handle. The httpx client is a stub since none of the methods
    under test go through query_as_user."""
    http_client = httpx.AsyncClient(
        base_url="http://stub",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )
    return ClickHouseDatabaseAdminHandle(
        client=ch_client,
        http_client=http_client,
        settings=ch_settings,
        database=database,
        username=username,
    )


def test_grant_reader_writer_admin_propagate_to_rights(ch_client, ch_settings, prefix):
    creator = f"{prefix}_creator"
    target = f"{prefix}_target"
    db = f"{prefix}_admin_grants"
    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    asyncio.run(creator_h.create_database(db))
    admin_h = _admin_handle(ch_client, ch_settings, database=db, username=creator)

    asyncio.run(admin_h.grant_reader(target))
    r = derive_rights(ch_client, username=target, groups=[])
    assert db in r.db_reader

    asyncio.run(admin_h.grant_writer(target))
    r = derive_rights(ch_client, username=target, groups=[])
    assert db in r.db_writer

    asyncio.run(admin_h.add_admin_user(target))
    r = derive_rights(ch_client, username=target, groups=[])
    assert db in r.db_admin


def test_revoke_clears_label(ch_client, ch_settings, prefix):
    creator = f"{prefix}_c"
    target = f"{prefix}_t"
    db = f"{prefix}_revoke_admin"
    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    asyncio.run(creator_h.create_database(db))
    admin_h = _admin_handle(ch_client, ch_settings, database=db, username=creator)
    asyncio.run(admin_h.grant_reader(target))
    asyncio.run(admin_h.revoke_reader(target))
    r = derive_rights(ch_client, username=target, groups=[])
    assert db not in r.db_reader


def test_delete_database_drops_tier_roles_and_db(ch_client, ch_settings, prefix):
    creator = f"{prefix}_c"
    db = f"{prefix}_to_drop"
    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    asyncio.run(creator_h.create_database(db))
    admin_h = _admin_handle(ch_client, ch_settings, database=db, username=creator)
    asyncio.run(admin_h.delete_database())

    db_rows = ch_client.query(
        "SELECT count() FROM system.databases WHERE name = {n:String}",
        parameters={"n": db},
    ).result_rows
    assert db_rows[0][0] == 0

    role_rows = ch_client.query(
        "SELECT count() FROM system.roles WHERE name LIKE {p:String}",
        parameters={"p": f"{db}\\_DB%"},
    ).result_rows
    assert role_rows[0][0] == 0


def test_list_admin_members_returns_creator(ch_client, ch_settings, prefix):
    creator = f"{prefix}_c"
    db = f"{prefix}_members"
    creator_h = ClickHouseDatabaseCreatorHandle(
        client=ch_client, settings=ch_settings, username=creator
    )
    asyncio.run(creator_h.create_database(db))
    admin_h = _admin_handle(ch_client, ch_settings, database=db, username=creator)
    members = asyncio.run(admin_h.list_admin_members())
    # Creator's _USER role should appear as a DBADMIN member.
    assert f"{creator}_USER" in members
