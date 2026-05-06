"""Unit tests for DatabaseAdminStore.

Tempfile DB. Each test gets a fresh store; teardown closes it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from iris.clickhouse.database_admins import DatabaseAdminStore


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "auth.db"


@pytest.fixture
def store(store_path):
    s = DatabaseAdminStore(path=str(store_path))
    s.bootstrap()
    yield s
    asyncio.run(s.close())


def test_is_admin_returns_false_on_empty(store):
    assert asyncio.run(
        store.is_admin(database="orders", username_lower="alice", roles=frozenset())
    ) is False


def test_add_admin_user_round_trips(store):
    asyncio.run(store.add_admin_user(database="orders", username="Alice"))
    assert asyncio.run(
        store.is_admin(database="orders", username_lower="alice", roles=frozenset())
    ) is True


def test_add_admin_user_lowercases(store):
    asyncio.run(store.add_admin_user(database="orders", username="ALICE"))
    rows = asyncio.run(store.list_admin_users(database="orders"))
    assert rows == ["alice"]


def test_add_admin_user_idempotent_across_case(store):
    asyncio.run(store.add_admin_user(database="orders", username="Alice"))
    asyncio.run(store.add_admin_user(database="orders", username="ALICE"))
    rows = asyncio.run(store.list_admin_users(database="orders"))
    assert rows == ["alice"]


def test_remove_admin_user(store):
    asyncio.run(store.add_admin_user(database="orders", username="alice"))
    asyncio.run(store.remove_admin_user(database="orders", username="ALICE"))
    rows = asyncio.run(store.list_admin_users(database="orders"))
    assert rows == []


def test_remove_admin_user_unknown_is_noop(store):
    asyncio.run(store.remove_admin_user(database="nope", username="ghost"))


def test_add_admin_role_round_trips(store):
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    assert asyncio.run(
        store.is_admin(
            database="orders", username_lower="bob", roles=frozenset({"ops"})
        )
    ) is True


def test_add_admin_role_idempotent(store):
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    rows = asyncio.run(store.list_admin_roles(database="orders"))
    assert rows == ["ops"]


def test_remove_admin_role(store):
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    asyncio.run(store.remove_admin_role(database="orders", role="ops"))
    rows = asyncio.run(store.list_admin_roles(database="orders"))
    assert rows == []


def test_is_admin_role_match(store):
    """Any role in the user's effective set that's listed for the DB grants admin."""
    asyncio.run(store.add_admin_role(database="orders", role="ops"))
    asyncio.run(store.add_admin_role(database="orders", role="leads"))
    assert asyncio.run(
        store.is_admin(
            database="orders", username_lower="x", roles=frozenset({"leads"})
        )
    ) is True


def test_is_admin_short_circuits_clickhouse_admin(store):
    """clickhouse_admin in roles -> admin of every database, no DB query needed."""
    assert asyncio.run(
        store.is_admin(
            database="orders",
            username_lower="x",
            roles=frozenset({"clickhouse_admin"}),
        )
    ) is True


def test_is_admin_isolation_per_database(store):
    asyncio.run(store.add_admin_user(database="orders", username="alice"))
    assert asyncio.run(
        store.is_admin(
            database="reports", username_lower="alice", roles=frozenset()
        )
    ) is False


def test_list_admin_users_per_database(store):
    asyncio.run(store.add_admin_user(database="orders", username="alice"))
    asyncio.run(store.add_admin_user(database="orders", username="bob"))
    asyncio.run(store.add_admin_user(database="reports", username="carol"))
    orders = asyncio.run(store.list_admin_users(database="orders"))
    reports = asyncio.run(store.list_admin_users(database="reports"))
    assert sorted(orders) == ["alice", "bob"]
    assert reports == ["carol"]


def test_close_is_idempotent(store_path):
    s = DatabaseAdminStore(path=str(store_path))
    asyncio.run(s.close())
    asyncio.run(s.close())
