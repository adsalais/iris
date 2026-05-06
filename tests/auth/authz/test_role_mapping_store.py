"""Unit tests for RoleMappingStore.

These tests use a tempfile DB. The store opens its own connection on
each test; the file persists for the duration of the test and is
cleaned up by tmp_path teardown.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from iris.auth.authz.mapping import RoleMapping, RoleMappingError
from iris.auth.authz.store import RoleMappingStore


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "auth.db"


@pytest.fixture
def store(store_path):
    s = RoleMappingStore(path=str(store_path))
    yield s
    asyncio.run(s.close())


def test_get_mapping_on_empty_db_returns_empty_mapping(store):
    mapping = asyncio.run(store.get_mapping())
    assert isinstance(mapping, RoleMapping)
    assert mapping.roles == {}
    assert mapping.closure == {}


def test_get_mapping_returns_seeded_role(store):
    # Seed via direct SQL (mutators are added in later tasks).
    store._conn.execute("INSERT INTO authz_roles(name) VALUES ('reader')")
    mapping = asyncio.run(store.get_mapping())
    assert "reader" in mapping.roles
    assert mapping.roles["reader"].groups == frozenset()
    assert mapping.roles["reader"].users_lower == frozenset()
    assert mapping.roles["reader"].includes == ()
    assert mapping.closure["reader"] == frozenset({"reader"})


def test_get_mapping_returns_groups_users_includes(store):
    c = store._conn
    c.execute("INSERT INTO authz_roles(name) VALUES ('reader')")
    c.execute("INSERT INTO authz_roles(name) VALUES ('writer')")
    c.execute("INSERT INTO authz_role_groups(role_name, group_name) VALUES ('writer', 'editors')")
    c.execute("INSERT INTO authz_role_users(role_name, username_lower) VALUES ('writer', 'bob')")
    c.execute("INSERT INTO authz_role_includes(role_name, included_role) VALUES ('writer', 'reader')")

    mapping = asyncio.run(store.get_mapping())

    assert mapping.roles["writer"].groups == frozenset({"editors"})
    assert mapping.roles["writer"].users_lower == frozenset({"bob"})
    assert mapping.roles["writer"].includes == ("reader",)
    assert mapping.closure["writer"] == frozenset({"reader", "writer"})
    assert mapping.closure["reader"] == frozenset({"reader"})


def test_get_mapping_users_lookup_is_case_insensitive_via_lowered_storage(store):
    """Users are stored lowercased; the existing resolve_roles lowercases the
    incoming username for comparison. So storage must already be lowercased."""
    c = store._conn
    c.execute("INSERT INTO authz_roles(name) VALUES ('admin')")
    c.execute(
        "INSERT INTO authz_role_users(role_name, username_lower) VALUES ('admin', 'alice')"
    )
    mapping = asyncio.run(store.get_mapping())
    assert "alice" in mapping.roles["admin"].users_lower


def test_close_is_idempotent(store_path):
    s = RoleMappingStore(path=str(store_path))
    asyncio.run(s.close())
    asyncio.run(s.close())  # must not raise


def test_schema_creates_indexes(store):
    """Sanity check the indexes the spec calls out."""
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_authz_%'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_authz_role_groups_group" in names
    assert "idx_authz_role_users_user" in names
    assert "idx_authz_role_includes_inc" in names


def test_schema_enforces_fk_on_includes(store):
    """included_role FK -- can't include a role that doesn't exist."""
    c = store._conn
    c.execute("INSERT INTO authz_roles(name) VALUES ('a')")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO authz_role_includes(role_name, included_role) VALUES ('a', 'nope')"
        )


def test_add_role_creates_row(store):
    asyncio.run(store.add_role("reader"))
    mapping = asyncio.run(store.get_mapping())
    assert "reader" in mapping.roles


def test_add_role_is_idempotent(store):
    asyncio.run(store.add_role("reader"))
    asyncio.run(store.add_role("reader"))  # must not raise
    mapping = asyncio.run(store.get_mapping())
    assert list(mapping.roles) == ["reader"]


def test_add_role_rejects_invalid_name(store):
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_role("bad name with spaces"))
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_role(""))
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_role("role!"))


def test_remove_role_deletes_row_and_cascades(store):
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_group_to_role("admin", "platform"))
    asyncio.run(store.add_user_to_role("admin", "alice"))
    asyncio.run(store.remove_role("admin"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles == {}
    assert store._conn.execute(
        "SELECT COUNT(*) FROM authz_role_groups"
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM authz_role_users"
    ).fetchone()[0] == 0


def test_remove_role_unknown_id_is_noop(store):
    asyncio.run(store.remove_role("not-a-role"))  # must not raise


def test_add_group_to_role_round_trips(store):
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_group_to_role("writer", "editors"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["writer"].groups == frozenset({"editors"})


def test_add_group_to_role_is_idempotent(store):
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_group_to_role("writer", "editors"))
    asyncio.run(store.add_group_to_role("writer", "editors"))  # no-op
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["writer"].groups == frozenset({"editors"})


def test_add_group_to_role_fails_if_role_missing(store):
    with pytest.raises(RoleMappingError):
        asyncio.run(store.add_group_to_role("nope", "editors"))


def test_remove_group_from_role(store):
    asyncio.run(store.add_role("writer"))
    asyncio.run(store.add_group_to_role("writer", "editors"))
    asyncio.run(store.remove_group_from_role("writer", "editors"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["writer"].groups == frozenset()


def test_add_user_to_role_lowercases(store):
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_user_to_role("admin", "Alice"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["admin"].users_lower == frozenset({"alice"})


def test_add_user_to_role_idempotent_across_case(store):
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_user_to_role("admin", "Alice"))
    asyncio.run(store.add_user_to_role("admin", "ALICE"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["admin"].users_lower == frozenset({"alice"})


def test_remove_user_from_role_lowercases(store):
    asyncio.run(store.add_role("admin"))
    asyncio.run(store.add_user_to_role("admin", "alice"))
    asyncio.run(store.remove_user_from_role("admin", "ALICE"))
    mapping = asyncio.run(store.get_mapping())
    assert mapping.roles["admin"].users_lower == frozenset()
