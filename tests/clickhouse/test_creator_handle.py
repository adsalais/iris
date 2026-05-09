"""Tests for DatabaseCreatorSession.create_database against the CH testcontainer.

Verifies that ``create_database`` provisions the database, the three tier
roles, and grants DBADMIN to the calling user — the lifecycle spelled out
in the spec under "CH-side state".
"""
import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from iris.auth.identity import User
from iris.auth.rights import EMPTY_CAPABILITIES
from iris.auth.views import DatabaseCreatorSession


def _stub_http() -> httpx.AsyncClient:
    """Stub http_client for sessions that don't actually need to make HTTP
    calls. The Session._ch() helper requires all three CH refs to be
    non-None, even if the method under test only uses ``client``."""
    return httpx.AsyncClient(
        base_url="http://stub",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"")),
    )


def _session_for(user: str, *, ch_client, ch_settings) -> DatabaseCreatorSession:
    now = datetime.now(UTC)
    u = User(subject=f"mock:{user}", username=user, display_name=user, groups=())
    return DatabaseCreatorSession(
        id="sid",
        user=u,
        created_at=now,
        expires_at=now + timedelta(hours=1),
        data={},
        capabilities=EMPTY_CAPABILITIES,
        client=ch_client,
        http_client=_stub_http(),
        settings=ch_settings,
        store=None,
    )


def test_create_database_creates_db_and_tier_roles(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator"
    db = f"{prefix}_owned"
    session = _session_for(user, ch_client=ch_client, ch_settings=ch_settings)
    asyncio.run(session.create_database(db))

    rows = ch_client.query(
        "SELECT count() FROM system.databases WHERE name = {n:String}",
        parameters={"n": db},
    ).result_rows
    assert rows[0][0] == 1

    role_rows = ch_client.query(
        "SELECT name FROM system.roles WHERE name LIKE {p:String}",
        parameters={"p": f"{db}\\_DB%"},
    ).result_rows
    role_names = {r[0] for r in role_rows}
    assert role_names == {f"{db}_DBADMIN", f"{db}_DBWRITER", f"{db}_DBREADER"}

    user_role = f"{user}_USER"
    granted = ch_client.query(
        """
        SELECT granted_role_name FROM system.role_grants
        WHERE role_name = {r:String}
        """,
        parameters={"r": user_role},
    ).result_rows
    assert any(g[0] == f"{db}_DBADMIN" for g in granted)


def test_create_database_idempotent(ch_client, ch_settings, prefix):
    user = f"{prefix}_creator2"
    db = f"{prefix}_idemp"
    session = _session_for(user, ch_client=ch_client, ch_settings=ch_settings)
    asyncio.run(session.create_database(db))
    asyncio.run(session.create_database(db))
