"""End-to-end integration tests: database creation + per-DB admin grants.

Reuses the session-scoped CH testcontainer in tests/clickhouse/conftest.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from iris.auth.authz.store import RoleMappingStore
from iris.clickhouse.database_admins import DatabaseAdminStore
from iris.clickhouse.handle import (
    ClickHouseDatabaseAdminHandle,
    ClickHouseDatabaseCreatorHandle,
    ClickHouseHandle,
)
from iris.clickhouse.users import init_user_rights


class _NoSeedSettings:
    bootstrap_role = "admin"
    bootstrap_user = None


def _http_client(ch_settings) -> httpx.AsyncClient:
    scheme = "https" if ch_settings.secure else "http"
    return httpx.AsyncClient(
        base_url=f"{scheme}://{ch_settings.host}:{ch_settings.port}",
        auth=(ch_settings.user, ch_settings.password),
        verify=ch_settings.verify,
        timeout=httpx.Timeout(30.0),
    )


def test_create_database_then_grant_then_read(
    ch_client, ch_settings, tmp_path: Path, prefix
) -> None:
    db_path = str(tmp_path / "auth.db")
    db_admin_store = DatabaseAdminStore(path=db_path)
    db_admin_store.bootstrap()
    authz_store = RoleMappingStore(path=db_path)
    authz_store.bootstrap(_NoSeedSettings())

    creator_username = f"{prefix}_creator"
    target_username = f"{prefix}_target"
    new_db = f"{prefix}_db"

    # Both users need CH accounts (init_user_rights would normally fire on login).
    init_user_rights(ch_client, username=creator_username, groups=[], settings=ch_settings)
    init_user_rights(ch_client, username=target_username, groups=[], settings=ch_settings)

    async def run():
        async with _http_client(ch_settings) as http_client:
            # Step 1: creator creates the database.
            creator_handle = ClickHouseDatabaseCreatorHandle(
                client=ch_client,
                settings=ch_settings,
                db_admin_store=db_admin_store,
                username=creator_username,
            )
            await creator_handle.create_database(new_db)

            # The creator should now be admin of this DB.
            assert await db_admin_store.is_admin(
                database=new_db,
                username_lower=creator_username.lower(),
                roles=frozenset(),
            )

            # Step 2: creator (now admin) grants read to the target user.
            admin_handle = ClickHouseDatabaseAdminHandle(
                client=ch_client,
                http_client=http_client,
                settings=ch_settings,
                db_admin_store=db_admin_store,
                authz_store=authz_store,
                database=new_db,
                username=creator_username,
            )
            await admin_handle.grant_select_to_user(target_username)

            # Step 3: a sample table for the target to read.
            await asyncio.to_thread(
                ch_client.command,
                f"CREATE TABLE IF NOT EXISTS `{new_db}`.t (n UInt32) ENGINE = MergeTree ORDER BY n",
            )
            await asyncio.to_thread(
                ch_client.command,
                f"INSERT INTO `{new_db}`.t VALUES (1), (2), (3)",
            )

            # Step 4: target user runs an impersonated SELECT.
            target_handle = ClickHouseHandle(
                client=ch_client, http_client=http_client, username=target_username
            )
            rows = await target_handle.query_as_user(
                f"SELECT n FROM `{new_db}`.t ORDER BY n"
            )
            assert rows == [{"n": 1}, {"n": 2}, {"n": 3}]

    try:
        asyncio.run(run())
    finally:
        asyncio.run(db_admin_store.close())
        asyncio.run(authz_store.close())


def test_non_admin_user_cannot_admin_database(
    ch_client, ch_settings, tmp_path: Path, prefix
) -> None:
    """A user not listed in the per-DB admins table can't grant or list admins."""
    db_path = str(tmp_path / "auth.db")
    db_admin_store = DatabaseAdminStore(path=db_path)
    db_admin_store.bootstrap()
    authz_store = RoleMappingStore(path=db_path)
    authz_store.bootstrap(_NoSeedSettings())

    db = f"{prefix}_other_db"
    other_user = f"{prefix}_outsider"

    asyncio.run(db_admin_store.add_admin_user(database=db, username="ownerlee"))

    try:
        admitted = asyncio.run(
            db_admin_store.is_admin(
                database=db,
                username_lower=other_user.lower(),
                roles=frozenset(),
            )
        )
        assert admitted is False
    finally:
        asyncio.run(db_admin_store.close())
        asyncio.run(authz_store.close())


def test_pre_existing_target_user_constraint(
    ch_client, ch_settings, tmp_path: Path, prefix
) -> None:
    """grant_select_to_user against a username that has never logged in
    fails because <username>_USER doesn't exist in CH yet."""
    db_path = str(tmp_path / "auth.db")
    db_admin_store = DatabaseAdminStore(path=db_path)
    db_admin_store.bootstrap()
    authz_store = RoleMappingStore(path=db_path)
    authz_store.bootstrap(_NoSeedSettings())

    db = f"{prefix}_pretest"
    creator = f"{prefix}_creator2"
    init_user_rights(ch_client, username=creator, groups=[], settings=ch_settings)

    # Create the DB + record creator as admin (parallel to ClickHouseDatabaseCreatorHandle).
    asyncio.run(asyncio.to_thread(ch_client.command, f"CREATE DATABASE IF NOT EXISTS `{db}`"))
    asyncio.run(db_admin_store.add_admin_user(database=db, username=creator))

    not_yet_logged_in = f"{prefix}_unborn"  # no CH user provisioned

    async def run():
        async with _http_client(ch_settings) as http_client:
            handle = ClickHouseDatabaseAdminHandle(
                client=ch_client,
                http_client=http_client,
                settings=ch_settings,
                db_admin_store=db_admin_store,
                authz_store=authz_store,
                database=db,
                username=creator,
            )
            with pytest.raises(Exception):
                # CH raises a DatabaseError; we don't translate it in this slice.
                await handle.grant_select_to_user(not_yet_logged_in)

    try:
        asyncio.run(run())
    finally:
        asyncio.run(db_admin_store.close())
        asyncio.run(authz_store.close())
