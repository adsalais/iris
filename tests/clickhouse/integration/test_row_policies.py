"""End-to-end: row policies actually filter what each user sees."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from iris.auth.views import (
    AdminSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
)
from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    refresh_capabilities,
    session_for,
)


def test_row_policy_filters_reader_but_not_admin(
    iris_app, keycloak_http, ch_client, prefix
):
    """Full chain: bob creates the database + table; carol's group is
    granted reader+writer; rows are inserted via iris_svc; alice (admin)
    adds a row policy ``has(tags, 'EU') TO readers_GRP``; dave (reader)
    queries via query_as_user and sees only EU rows; iris_svc (which
    holds iris_global_admin via bootstrap) queries the same table and
    sees all 4 rows."""
    db = f"test_db_{prefix}"

    async def _run() -> list[dict[str, object]]:
        with TestClient(iris_app) as test_client:
            # alice: needed only to add the row policy.
            alice_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="alice",
                password="secret",
            )

            # bob: create + grant.
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
            await refresh_capabilities(iris_app, bob_sid)
            bob_admin = await session_for(
                iris_app, bob_sid, kind="database_admin", database=db
            )
            assert isinstance(bob_admin, DatabaseAdminSession)
            await bob_admin.grant_writer_to_group("writers")
            await bob_admin.grant_reader_to_group("readers")

            # Insert via iris_svc (CH's EXECUTE AS doesn't support INSERT).
            insert_sql = (
                f"INSERT INTO `{db}`.records (id, region, tags, score, active, "
                + "created_at, measured_at, birthday, note, counts) VALUES "
                + "(1, 'EU', ['EU','UK'], 1.0, true, '2026-05-09 12:00:00', "
                + "'2026-05-09 12:00:00.100', '2026-05-09', NULL, [1]), "
                + "(2, 'EU', ['EU','DE'], 2.0, true, '2026-05-09 12:01:00', "
                + "'2026-05-09 12:01:00.200', '2026-05-09', NULL, [2]), "
                + "(3, 'US', ['US'],      3.0, true, '2026-05-09 12:02:00', "
                + "'2026-05-09 12:02:00.300', '2026-05-09', NULL, [3]), "
                + "(4, 'CA', ['CA'],      4.0, true, '2026-05-09 12:03:00', "
                + "'2026-05-09 12:03:00.400', '2026-05-09', NULL, [4])"
            )
            ch_client.command(insert_sql)

            # alice: add the row policy on tags for readers_GRP.
            alice_admin = await session_for(iris_app, alice_sid, kind="admin")
            assert isinstance(alice_admin, AdminSession)
            await alice_admin.add_row_policy(
                database=db, table="records",
                column="tags", role="readers_GRP", value="EU",
            )

            # dave: query as reader — should see only EU rows.
            dave_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="dave",
                password="dave-pw",
            )
            dave_reader = await session_for(
                iris_app, dave_sid, kind="database_reader", database=db
            )
            assert isinstance(dave_reader, DatabaseSession)
            return await dave_reader.query_as_user(
                "SELECT id FROM records ORDER BY id"
            )

    rows = asyncio.run(_run())
    assert rows == [{"id": 1}, {"id": 2}], f"reader saw: {rows}"

    # iris_svc holds iris_global_admin via bootstrap_admin's seed; the
    # USING-1 wildcard policy admits this read of all 4 rows.
    all_rows = ch_client.query(
        f"SELECT id FROM `{db}`.records ORDER BY id"
    ).result_rows
    assert [r[0] for r in all_rows] == [1, 2, 3, 4]
