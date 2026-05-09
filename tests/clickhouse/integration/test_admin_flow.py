"""End-to-end: alice (global admin) runs audit + introspection
operations via AdminSession. Verifies the role/grant/policy graph is
consistent after a typical setup."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from iris.auth.views import (
    AdminSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
)
from tests.clickhouse.integration._helpers import (
    TABLE_DDL,
    login_as,
    refresh_capabilities,
    session_for,
)


def test_admin_audit_queries_return_consistent_state(
    iris_app, keycloak_http, ch_client, prefix
):
    db = f"test_db_{prefix}"

    async def _run() -> dict[str, object]:
        with TestClient(iris_app) as test_client:
            # Setup chain.
            alice_sid = login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="alice",
                password="secret",
            )
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

            # carol logs in so writers_GRP and her per-user role are
            # provisioned in CH — audit reads need her CH-side identity
            # to exist.
            login_as(
                test_client=test_client,
                keycloak_http=keycloak_http,
                username="carol",
                password="carol-pw",
            )

            # alice: AdminSession reads.
            alice_admin = await session_for(iris_app, alice_sid, kind="admin")
            assert isinstance(alice_admin, AdminSession)
            user_grants = await alice_admin.user_grants(username="carol")
            # writers_GRP itself only has role-grants (it inherits from
            # the tier role test_db_DBWRITER); the direct DB-scope grants
            # live on the tier role. role_grants on the tier role surfaces
            # the SELECT/INSERT/ALTER UPDATE the DBWRITER tier holds.
            tier_role_grants = await alice_admin.role_grants(
                role=f"{db}_DBWRITER"
            )
            user_roles = await alice_admin.user_role_memberships(
                username="carol"
            )
            await alice_admin.add_row_policy(
                database=db, table="records",
                column="tags", role="readers_GRP", value="EU",
            )
            table_policies = await alice_admin.table_row_policies(
                database=db, table="records"
            )

            # bob_admin (DatabaseAdminSession): list_members on the db.
            members = await bob_admin.list_members()

            return {
                "user_grants": user_grants,
                "tier_role_grants": tier_role_grants,
                "user_roles": user_roles,
                "table_policies": table_policies,
                "members": members,
            }

    out = asyncio.run(_run())

    # carol's role chain includes writers_GRP and carol_USER.
    user_roles = out["user_roles"]
    assert isinstance(user_roles, list)
    role_names = {row["granted_role_name"] for row in user_roles}
    assert "writers_GRP" in role_names, role_names
    assert "carol_USER" in role_names, role_names

    # The tier role test_db_DBWRITER holds the actual DB-scope grants
    # (SELECT/INSERT/ALTER UPDATE). writers_GRP only has a role-membership
    # entry (writers_GRP -> test_db_DBWRITER) which lives in
    # system.role_grants, not system.grants.
    tier_role_grants = out["tier_role_grants"]
    assert isinstance(tier_role_grants, list)
    assert any(
        row.get("database") == db for row in tier_role_grants
    ), f"{db}_DBWRITER should have DB-scope grants; got {tier_role_grants}"

    # The row policy alice just added is visible on the table.
    table_policies = out["table_policies"]
    assert isinstance(table_policies, list)
    short_names = {row["short_name"] for row in table_policies}
    assert any(
        sn.startswith(f"{db}_records_readers_GRP_EU_") for sn in short_names
    ), f"row policy not found in {short_names}"

    # bob's list_members returns {admin, reader, writer} dicts of
    # {"kind": "user"|"role", "name": ...} entries. bob's per-user role
    # bob_USER got DBADMIN granted on create_database.
    members = out["members"]
    assert isinstance(members, dict)
    assert set(members.keys()) == {"admin", "reader", "writer"}
    assert any(
        m.get("kind") == "role" and m.get("name") == "bob_USER"
        for m in members["admin"]
    ), f"bob_USER missing from admin tier: {members}"

    # user_grants shape is the system.grants rows directly attached to
    # the carol user account (none in our setup — grants are on roles).
    user_grants = out["user_grants"]
    assert isinstance(user_grants, list)
