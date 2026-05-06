"""install_authz_schema seeds the admin role + clickhouse_admin + bootstrap user
on first install only. Subsequent installs (tables exist) leave content alone.
"""
from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from iris.auth.authz.bootstrap import install_authz_schema
from iris.auth.authz.store import RoleMappingStore


@dataclass(frozen=True)
class _StubSettings:
    bootstrap_role: str = "admin"
    bootstrap_user: str | None = "alice"


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def test_first_install_seeds_admin_with_clickhouse_admin_include(tmp_path: Path):
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings())

        roles = {
            r["name"] for r in conn.execute("SELECT name FROM authz_roles").fetchall()
        }
        assert roles == {"admin", "clickhouse_admin"}

        includes = conn.execute(
            "SELECT role_name, included_role FROM authz_role_includes"
        ).fetchall()
        assert [(r["role_name"], r["included_role"]) for r in includes] == [
            ("admin", "clickhouse_admin")
        ]

        users = conn.execute(
            "SELECT role_name, username_lower FROM authz_role_users"
        ).fetchall()
        assert [(r["role_name"], r["username_lower"]) for r in users] == [
            ("admin", "alice")
        ]
    finally:
        conn.close()


def test_second_install_is_noop_even_with_changed_settings(tmp_path: Path):
    """Once tables exist, the bootstrap function leaves content alone."""
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings(bootstrap_user="alice"))
        # Operator removes alice via mutator API.
        conn.execute(
            "DELETE FROM authz_role_users WHERE username_lower = 'alice'"
        )
        # Restart with a different bootstrap user — should NOT re-seed.
        install_authz_schema(
            conn, _StubSettings(bootstrap_user="bob")
        )

        users = conn.execute(
            "SELECT username_lower FROM authz_role_users"
        ).fetchall()
        assert users == []  # alice gone; bob NOT added
    finally:
        conn.close()


def test_bootstrap_user_unset_skips_seeding(tmp_path: Path):
    """Fresh DB but operator chose not to seed."""
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings(bootstrap_user=None))

        roles = conn.execute("SELECT name FROM authz_roles").fetchall()
        assert roles == []
        users = conn.execute("SELECT * FROM authz_role_users").fetchall()
        assert users == []
    finally:
        conn.close()


def test_custom_bootstrap_role_name(tmp_path: Path):
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(
            conn, _StubSettings(bootstrap_role="superuser", bootstrap_user="alice")
        )
        roles = {
            r["name"] for r in conn.execute("SELECT name FROM authz_roles").fetchall()
        }
        assert roles == {"superuser", "clickhouse_admin"}
        includes = conn.execute(
            "SELECT role_name, included_role FROM authz_role_includes"
        ).fetchall()
        assert [(r["role_name"], r["included_role"]) for r in includes] == [
            ("superuser", "clickhouse_admin")
        ]
    finally:
        conn.close()


def test_username_lowercased(tmp_path: Path):
    conn = _open(tmp_path / "auth.db")
    try:
        install_authz_schema(conn, _StubSettings(bootstrap_user="Alice"))
        users = conn.execute(
            "SELECT username_lower FROM authz_role_users"
        ).fetchall()
        assert [r["username_lower"] for r in users] == ["alice"]
    finally:
        conn.close()


def test_clickhouse_admin_string_matches_clickhouse_module():
    """Drift check: the hardcoded string in bootstrap.py must match the
    constant in iris.clickhouse.deps. If clickhouse renames the constant,
    this test fails and the bootstrap must be updated."""
    from iris.auth.authz import bootstrap
    from iris.clickhouse.deps import CLICKHOUSE_ADMIN_ROLE

    assert bootstrap._CLICKHOUSE_ADMIN_ROLE == CLICKHOUSE_ADMIN_ROLE


def test_works_with_role_mapping_store_after_bootstrap(tmp_path: Path):
    """End-to-end: install_authz_schema then use a RoleMappingStore against
    the same DB; the seeded data is visible via get_mapping."""
    db_path = tmp_path / "auth.db"
    conn = _open(db_path)
    try:
        install_authz_schema(conn, _StubSettings())
    finally:
        conn.close()

    store = RoleMappingStore(path=str(db_path))
    try:
        mapping = asyncio.run(store.get_mapping())
        assert "admin" in mapping.roles
        assert "clickhouse_admin" in mapping.roles
        assert mapping.roles["admin"].includes == ("clickhouse_admin",)
        assert mapping.roles["admin"].users_lower == frozenset({"alice"})
        assert mapping.closure["admin"] == frozenset({"admin", "clickhouse_admin"})
    finally:
        asyncio.run(store.close())
