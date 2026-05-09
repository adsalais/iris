"""Tests for DatabaseAdminSession: tier grants/revokes plus delete_database."""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from iris.auth.identity import User
from iris.auth.rights import EMPTY_CAPABILITIES
from iris.auth.views import DatabaseAdminSession, DatabaseCreatorSession
from iris.clickhouse.capabilities import derive_capabilities


def _admin_session(
    ch_client, ch_settings, *, database: str, username: str
) -> DatabaseAdminSession:
    now = datetime.now(UTC)
    u = User(subject=f"mock:{username}", username=username, display_name=username, groups=())
    http_client = httpx.AsyncClient(
        base_url="http://stub",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )
    return DatabaseAdminSession(
        id="sid",
        user=u,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        capabilities=EMPTY_CAPABILITIES,
        client=ch_client,
        http_client=http_client,
        settings=ch_settings,
        store=None,
        database=database,
    )


def _creator_session(
    ch_client, ch_settings, *, username: str
) -> DatabaseCreatorSession:
    now = datetime.now(UTC)
    u = User(subject=f"mock:{username}", username=username, display_name=username, groups=())
    return DatabaseCreatorSession(
        id="sid",
        user=u,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        capabilities=EMPTY_CAPABILITIES,
        client=ch_client,
        http_client=httpx.AsyncClient(
            base_url="http://stub",
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
        ),
        settings=ch_settings,
        store=None,
    )


def test_grant_reader_writer_admin_propagate_to_capabilities(ch_client, ch_settings, prefix):
    creator = f"{prefix}_creator"
    target = f"{prefix}_target"
    db = f"{prefix}_admin_grants"
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)

    asyncio.run(admin.grant_reader(target))
    c = derive_capabilities(ch_client, username=target, groups=[])
    assert db in c.db_reader

    asyncio.run(admin.grant_writer(target))
    c = derive_capabilities(ch_client, username=target, groups=[])
    assert db in c.db_writer

    asyncio.run(admin.add_admin_user(target))
    c = derive_capabilities(ch_client, username=target, groups=[])
    assert db in c.db_admin


def test_revoke_clears_label(ch_client, ch_settings, prefix):
    creator = f"{prefix}_c"
    target = f"{prefix}_t"
    db = f"{prefix}_revoke_admin"
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)
    asyncio.run(admin.grant_reader(target))
    asyncio.run(admin.revoke_reader(target))
    c = derive_capabilities(ch_client, username=target, groups=[])
    assert db not in c.db_reader


def test_delete_database_drops_tier_roles_and_db(ch_client, ch_settings, prefix):
    creator = f"{prefix}_c"
    db = f"{prefix}_to_drop"
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)
    asyncio.run(admin.delete_database())

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
    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)
    members = asyncio.run(admin.list_admin_members())
    # Creator is granted DBADMIN to its <username>_USER role (not directly
    # to the user account), so the entry is kind="role" with the per-user
    # role name.
    assert {"kind": "role", "name": f"{creator}_USER"} in members


def test_list_admin_members_includes_direct_user_grant(
    ch_client, ch_settings, prefix
):
    """A user account granted the admin role directly (not via _USER role)
    appears with kind='user'."""
    creator = f"{prefix}_c2"
    db = f"{prefix}_members2"
    direct_user = f"{prefix}_direct"

    asyncio.run(
        _creator_session(ch_client, ch_settings, username=creator).create_database(db)
    )
    ch_client.command(
        f"CREATE USER IF NOT EXISTS `{direct_user}` IDENTIFIED WITH no_password"
    )
    ch_client.command(f"GRANT `{db}_DBADMIN` TO `{direct_user}`")

    admin = _admin_session(ch_client, ch_settings, database=db, username=creator)
    members = asyncio.run(admin.list_admin_members())
    assert {"kind": "user", "name": direct_user} in members
