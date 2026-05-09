"""End-to-end: a user in writers group can insert; reader cannot."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthForbidden
from iris.auth.identity import (
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
)
from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    refresh_rights,
    session_for,
)


def test_writer_session_can_select_inserted_rows(
    iris_app, keycloak_http, ch_client, prefix
):
    """bob creates the database, grants writer to writers_GRP, then
    rows are inserted via the iris_svc client. carol logs in as a
    writer and can SELECT them via the impersonated path. (CH's
    ``EXECUTE AS`` only supports SELECT — INSERT under EXECUTE AS
    raises SYNTAX_ERROR — so insertion in iris production today goes
    through a non-impersonated path; the writer session's read
    capability is what query_as_user exercises end-to-end.)"""
    db = f"test_db_{prefix}"

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            # bob's login rights have can_create_database, but db_admin is
            # empty because the database doesn't exist yet.
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

            # create_database auto-granted DBADMIN to bob_USER. Refresh
            # bob's stored rights view (mirrors what the post-login hook
            # does) so a fresh DatabaseAdminSession can be constructed
            # without a second Keycloak round-trip.
            await refresh_rights(iris_app, bob_sid)
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            assert isinstance(bob_admin, DatabaseAdminSession)
            await bob_admin.grant_writer_to_group("writers")

            # carol: log in (provisions writers_GRP grant for her), then
            # query as writer.
            carol_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="carol",
                password="carol-pw",
            )
            carol_writer = await session_for(
                iris_app, carol_sid, kind="database_writer", database=db
            )
            assert isinstance(carol_writer, DatabaseSession)
            assert db in carol_writer.rights.db_writer

            # SELECT works as carol; INSERT under EXECUTE AS is not
            # supported by ClickHouse (returns SYNTAX_ERROR right after
            # VALUES) — only SELECT is. Insert via ch_client (iris_svc),
            # then verify carol's read path.
            insert_sql = (
                f"INSERT INTO `{db}`.records (id, region, tags, score, active, "
                + "created_at, measured_at, birthday, note, counts) VALUES "
                + "(1, 'EU', ['EU','UK'], 1.5, true, '2026-05-09 12:00:00', "
                + "'2026-05-09 12:00:00.123', '2026-05-09', 'first', [1,NULL,3]), "
                + "(2, 'EU', ['EU','DE'], 2.5, true, '2026-05-09 12:01:00', "
                + "'2026-05-09 12:01:00.456', '2026-05-09', 'second', [4,5]), "
                + "(3, 'US', ['US'],      3.5, false,'2026-05-09 12:02:00', "
                + "'2026-05-09 12:02:00.789', '2026-05-09', NULL, [7]), "
                + "(4, 'CA', ['CA'],      4.5, true, '2026-05-09 12:03:00', "
                + "'2026-05-09 12:03:00.000', '2026-05-09', 'fourth', [])"
            )
            ch_client.command(insert_sql)

            # Verify carol's writer-tier session can SELECT (the SELECT
            # privilege flows from DBWRITER, granted to writers_GRP and
            # thus carol_USER).
            rows = await carol_writer.query_as_user(
                "SELECT count() AS n FROM records"
            )
            assert rows == [{"n": 4}], f"carol expected to see 4 rows; got {rows}"

    asyncio.run(_run())

    # Verify the rows landed.
    rows = ch_client.query(
        f"SELECT count() FROM `{db}`.records"
    ).result_rows
    assert rows[0][0] == 4, f"expected 4 rows in {db}.records, got {rows[0][0]}"


def test_reader_cannot_take_writer_session(iris_app, keycloak_http, prefix):
    """dave is in readers, not writers. Even after bob grants writer to
    writers_GRP, dave's writer-session resolution fails with AuthForbidden."""
    db = f"test_db_{prefix}_readonly"

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            # bob: create db, grant writer-tier (just to set the scene; dave's
            # exclusion comes from his groups, not the absence of grants).
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
            # Refresh bob's rights so he picks up the freshly-granted DBADMIN.
            await refresh_rights(iris_app, bob_sid)
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            assert isinstance(bob_admin, DatabaseAdminSession)
            await bob_admin.grant_writer_to_group("writers")

            # dave: log in, attempt writer-session on the same database.
            dave_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="dave",
                password="dave-pw",
            )
            try:
                await session_for(
                    iris_app, dave_sid, kind="database_writer", database=db
                )
            except AuthForbidden:
                return
            raise AssertionError("AuthForbidden should have been raised")

    asyncio.run(_run())
