"""End-to-end tier promotion: creator creates DB, grants writer to bob,
bob's session shows db_writer (but not db_admin)."""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from iris.auth.identity import User
from iris.auth.rights import EMPTY_CAPABILITIES
from iris.auth.views import DatabaseAdminSession, DatabaseCreatorSession
from iris.clickhouse.capabilities import derive_capabilities
from iris.clickhouse.users import provision_user


def _stub_http():
    return httpx.AsyncClient(
        base_url="http://stub",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )


def _user(name: str) -> User:
    return User(subject=f"mock:{name}", username=name, display_name=name, groups=())


def test_creator_grants_writer_promotes_target(ch_client, ch_settings, prefix):
    creator = f"{prefix}_creator"
    bob = f"{prefix}_bob"
    db = f"{prefix}_promo"
    now = datetime.now(UTC)

    creator_s = DatabaseCreatorSession(
        id="sid", user=_user(creator), created_at=now,
        expires_at=now + timedelta(hours=1), data={}, capabilities=EMPTY_CAPABILITIES,
        client=ch_client, http_client=_stub_http(), settings=ch_settings, store=None,
    )
    asyncio.run(creator_s.create_database(db))

    provision_user(ch_client, username=bob, groups=[], settings=ch_settings)
    bob_caps_before = derive_capabilities(ch_client, username=bob, groups=[])
    assert db not in bob_caps_before.db_writer

    admin_s = DatabaseAdminSession(
        id="sid", user=_user(creator), created_at=now,
        expires_at=now + timedelta(hours=1), data={}, capabilities=EMPTY_CAPABILITIES,
        client=ch_client, http_client=_stub_http(), settings=ch_settings,
        store=None, database=db,
    )
    asyncio.run(admin_s.grant_writer(bob))

    bob_caps_after = derive_capabilities(ch_client, username=bob, groups=[])
    assert db in bob_caps_after.db_writer
    assert db not in bob_caps_after.db_admin
    assert bob_caps_after.has_read(db)
    assert bob_caps_after.has_write(db)
    assert not bob_caps_after.has_admin(db)


def test_creator_is_immediately_db_admin(ch_client, ch_settings, prefix):
    creator = f"{prefix}_solo_creator"
    db = f"{prefix}_solo"
    now = datetime.now(UTC)

    creator_s = DatabaseCreatorSession(
        id="sid", user=_user(creator), created_at=now,
        expires_at=now + timedelta(hours=1), data={}, capabilities=EMPTY_CAPABILITIES,
        client=ch_client, http_client=_stub_http(), settings=ch_settings, store=None,
    )
    asyncio.run(creator_s.create_database(db))
    provision_user(ch_client, username=creator, groups=[], settings=ch_settings)
    capabilities = derive_capabilities(ch_client, username=creator, groups=[])
    assert db in capabilities.db_admin
    assert capabilities.has_admin(db)
    assert capabilities.has_write(db)
    assert capabilities.has_read(db)
