"""End-to-end: revoke + delete operations work and propagate."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthForbidden
from iris.auth.identity import (
    DatabaseAdminSession,
    DatabaseCreatorSession,
)
from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    refresh_rights,
    session_for,
)


def test_revoke_writer_drops_writer_rights_on_next_login(
    iris_app, keycloak_http, ch_client, prefix
):
    """After bob revokes writer-tier from writers_GRP, carol's NEXT
    refresh_rights derives an empty db_writer (no fresh Keycloak
    round-trip needed; the post-login hook semantics are reproduced
    by refresh_rights). The session_for(database_writer) call then
    raises AuthForbidden."""
    db = f"test_db_{prefix}"

    async def _run() -> bool:
        with TestClient(iris_app) as test_client:
            bob_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            creator = await session_for(
                iris_app, bob_sid, kind="database_creator"
            )
            assert isinstance(creator, DatabaseCreatorSession)
            await creator.create_database(db)
            ch_client.command(TABLE_DDL.format(db=db))
            await refresh_rights(iris_app, bob_sid)
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            assert isinstance(bob_admin, DatabaseAdminSession)
            await bob_admin.grant_writer_to_group("writers")

            # carol logs in: confirm she is a writer.
            carol_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="carol",
                password="carol-pw",
            )
            carol_first = await session_for(
                iris_app, carol_sid, kind="database_writer", database=db
            )
            assert db in carol_first.rights.db_writer

            # bob revokes writer; carol's stored rights are stale until
            # the next login (or refresh_rights). Refresh and verify her
            # writer-session resolution now fails with AuthForbidden.
            await bob_admin.revoke_writer_from_group("writers")
            await refresh_rights(iris_app, carol_sid)
            try:
                await session_for(
                    iris_app, carol_sid, kind="database_writer", database=db
                )
            except AuthForbidden:
                return True
            return False

    raised = asyncio.run(_run())
    assert raised, "carol's writer-session should have been forbidden after revoke"


def test_delete_database_drops_db_and_tier_roles(
    iris_app, keycloak_http, ch_client, prefix
):
    """bob.delete_database() drops the database AND its three tier roles."""
    db = f"test_db_{prefix}_doomed"

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            bob_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            creator = await session_for(
                iris_app, bob_sid, kind="database_creator"
            )
            assert isinstance(creator, DatabaseCreatorSession)
            await creator.create_database(db)
            await refresh_rights(iris_app, bob_sid)
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            assert isinstance(bob_admin, DatabaseAdminSession)
            await bob_admin.delete_database()

    asyncio.run(_run())

    # database gone
    db_count = ch_client.query(
        "SELECT count() FROM system.databases WHERE name = {n:String}",
        parameters={"n": db},
    ).result_rows[0][0]
    assert db_count == 0, f"database {db} still present"

    # tier roles gone
    role_count = ch_client.query(
        "SELECT count() FROM system.roles WHERE name LIKE {p:String}",
        parameters={"p": f"{db}\\_DB%"},
    ).result_rows[0][0]
    assert role_count == 0, f"tier roles for {db} still present"
