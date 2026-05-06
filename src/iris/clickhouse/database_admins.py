"""Per-database admin store for ClickHouse.

Holds two tables in the auth SQLite DB (same file as sessions and authz_*):

    CREATE TABLE clickhouse_database_admins_users (
        database_name  TEXT NOT NULL,
        username_lower TEXT NOT NULL,
        PRIMARY KEY (database_name, username_lower)
    );
    CREATE TABLE clickhouse_database_admins_roles (
        database_name  TEXT NOT NULL,
        role_name      TEXT NOT NULL,
        PRIMARY KEY (database_name, role_name)
    );

is_admin short-circuits to True when ``clickhouse_admin`` is in the
session's effective roles — global admins admin every database
without a per-DB row.
"""
from __future__ import annotations

import asyncio
import sqlite3

# Must match iris.clickhouse.deps.CLICKHOUSE_ADMIN_ROLE — the global
# admin role short-circuits is_admin without needing a per-DB row.
_GLOBAL_ADMIN_ROLE = "clickhouse_admin"

_DB_ADMIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS clickhouse_database_admins_users (
    database_name  TEXT NOT NULL,
    username_lower TEXT NOT NULL,
    PRIMARY KEY (database_name, username_lower)
);

CREATE TABLE IF NOT EXISTS clickhouse_database_admins_roles (
    database_name  TEXT NOT NULL,
    role_name      TEXT NOT NULL,
    PRIMARY KEY (database_name, role_name)
);

CREATE INDEX IF NOT EXISTS idx_ch_db_admins_users_user
    ON clickhouse_database_admins_users(username_lower);
CREATE INDEX IF NOT EXISTS idx_ch_db_admins_roles_role
    ON clickhouse_database_admins_roles(role_name);
"""


class DatabaseAdminStore:
    def __init__(self, *, path: str) -> None:
        self._lock = asyncio.Lock()
        self._closed = False
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_pragmas()

    def _init_pragmas(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def bootstrap(self) -> None:
        """Create the schema. Idempotent. Synchronous: called from
        iris.clickhouse.install at app construction (before any request
        loop). With :memory: the same connection used for queries must
        also create the schema, so this method is the only place the
        schema is created."""
        self._conn.executescript(_DB_ADMIN_SCHEMA)

    async def is_admin(
        self, *, database: str, username_lower: str, roles: frozenset[str]
    ) -> bool:
        if _GLOBAL_ADMIN_ROLE in roles:
            return True
        async with self._lock:
            return await asyncio.to_thread(
                self._is_admin_sync, database, username_lower, roles
            )

    def _is_admin_sync(
        self, database: str, username_lower: str, roles: frozenset[str]
    ) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM clickhouse_database_admins_users WHERE database_name = ? AND username_lower = ?",
            (database, username_lower),
        ).fetchone()
        if row is not None:
            return True
        if not roles:
            return False
        placeholders = ",".join("?" * len(roles))
        row = self._conn.execute(
            f"SELECT 1 FROM clickhouse_database_admins_roles WHERE database_name = ? AND role_name IN ({placeholders}) LIMIT 1",
            (database, *sorted(roles)),
        ).fetchone()
        return row is not None

    async def add_admin_user(self, *, database: str, username: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "INSERT OR IGNORE INTO clickhouse_database_admins_users(database_name, username_lower) VALUES (?, ?)",
                (database, username.lower()),
            )

    async def remove_admin_user(self, *, database: str, username: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM clickhouse_database_admins_users WHERE database_name = ? AND username_lower = ?",
                (database, username.lower()),
            )

    async def add_admin_role(self, *, database: str, role: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "INSERT OR IGNORE INTO clickhouse_database_admins_roles(database_name, role_name) VALUES (?, ?)",
                (database, role),
            )

    async def remove_admin_role(self, *, database: str, role: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM clickhouse_database_admins_roles WHERE database_name = ? AND role_name = ?",
                (database, role),
            )

    async def list_admin_users(self, *, database: str) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: [
                    r["username_lower"]
                    for r in self._conn.execute(
                        "SELECT username_lower FROM clickhouse_database_admins_users WHERE database_name = ? ORDER BY username_lower",
                        (database,),
                    ).fetchall()
                ]
            )

    async def list_admin_roles(self, *, database: str) -> list[str]:
        async with self._lock:
            return await asyncio.to_thread(
                lambda: [
                    r["role_name"]
                    for r in self._conn.execute(
                        "SELECT role_name FROM clickhouse_database_admins_roles WHERE database_name = ? ORDER BY role_name",
                        (database,),
                    ).fetchall()
                ]
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._conn.close)
