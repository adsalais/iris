"""End-to-end: a user with the creators group creates a database
and a many-typed table; a user without the creators group can't."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthForbidden
from iris.auth.identity import DatabaseCreatorSession
from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    session_for,
)


def test_creator_can_create_database_and_table(
    iris_app, keycloak_http, ch_client, prefix
):
    """bob (creators group + global CREATE DATABASE grant) creates a
    database via DatabaseCreatorSession.create_database. The records
    table is created via ch_client (iris_svc) because the table DDL
    isn't bob-scope work and bob's tier-admin client isn't directly
    exposed to test code."""
    db = f"test_db_{prefix}"

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="bob",
                password="hunter2",
            )
            creator = await session_for(
                iris_app, sid, kind="database_creator"
            )
            assert isinstance(creator, DatabaseCreatorSession)
            assert creator.rights.can_create_database is True
            await creator.create_database(db)

    asyncio.run(_run())

    # Create the records table via ch_client (iris_svc). bob is DBADMIN
    # of the new database via create_database; iris_svc has the
    # necessary privileges from the testcontainer setup.
    ch_client.command(TABLE_DDL.format(db=db))

    db_rows = ch_client.query(
        "SELECT count() FROM system.databases WHERE name = {n:String}",
        parameters={"n": db},
    ).result_rows
    assert db_rows[0][0] == 1, f"database {db} not present"
    table_rows = ch_client.query(
        "SELECT count() FROM system.tables WHERE database = {d:String} AND name = 'records'",
        parameters={"d": db},
    ).result_rows
    assert table_rows[0][0] == 1, f"table {db}.records not present"


def test_non_creator_cannot_take_database_creator_session(
    iris_app, keycloak_http
):
    """dave is in `readers`, not `creators`. session_for raises
    AuthForbidden at construction time — the same gate iris.auth.deps
    enforces on the HTTP route layer."""

    async def _run() -> None:
        with TestClient(iris_app) as test_client:
            sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="dave",
                password="dave-pw",
            )
            try:
                await session_for(iris_app, sid, kind="database_creator")
            except AuthForbidden:
                return
            raise AssertionError("AuthForbidden should have been raised")

    asyncio.run(_run())
