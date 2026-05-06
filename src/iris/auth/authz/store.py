"""SQLite-backed role mapping store.

Opens its own sqlite3.Connection against the auth DB file (same file as
SessionStore; WAL mode handles coexistence). All sync sqlite3 calls are
wrapped in asyncio.to_thread.

This file ships the read path + schema bootstrap. Mutators land in
later commits.
"""
from __future__ import annotations

import asyncio
import sqlite3

from iris.auth.authz.mapping import (
    RoleDef,
    RoleMapping,
    _compute_closure,
)

_AUTHZ_SCHEMA = """
CREATE TABLE IF NOT EXISTS authz_roles (
    name TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS authz_role_groups (
    role_name  TEXT NOT NULL,
    group_name TEXT NOT NULL,
    PRIMARY KEY (role_name, group_name),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS authz_role_users (
    role_name      TEXT NOT NULL,
    username_lower TEXT NOT NULL,
    PRIMARY KEY (role_name, username_lower),
    FOREIGN KEY (role_name) REFERENCES authz_roles(name) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS authz_role_includes (
    role_name     TEXT NOT NULL,
    included_role TEXT NOT NULL,
    PRIMARY KEY (role_name, included_role),
    FOREIGN KEY (role_name)     REFERENCES authz_roles(name) ON DELETE CASCADE,
    FOREIGN KEY (included_role) REFERENCES authz_roles(name) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_authz_role_groups_group ON authz_role_groups(group_name);
CREATE INDEX IF NOT EXISTS idx_authz_role_users_user   ON authz_role_users(username_lower);
CREATE INDEX IF NOT EXISTS idx_authz_role_includes_inc ON authz_role_includes(included_role);
"""


class RoleMappingStore:
    def __init__(self, *, path: str) -> None:
        self._lock = asyncio.Lock()
        self._closed = False
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        # Same PRAGMAs as SessionStore. journal_mode=WAL is file-level and
        # idempotent. synchronous + busy_timeout are connection-level and
        # need to be set on this connection too.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_AUTHZ_SCHEMA)

    async def get_mapping(self) -> RoleMapping:
        async with self._lock:
            return await asyncio.to_thread(self._get_mapping_sync)

    def _get_mapping_sync(self) -> RoleMapping:
        role_rows = self._conn.execute(
            "SELECT name FROM authz_roles"
        ).fetchall()
        group_rows = self._conn.execute(
            "SELECT role_name, group_name FROM authz_role_groups"
        ).fetchall()
        user_rows = self._conn.execute(
            "SELECT role_name, username_lower FROM authz_role_users"
        ).fetchall()
        include_rows = self._conn.execute(
            "SELECT role_name, included_role FROM authz_role_includes "
            "ORDER BY role_name, included_role"
        ).fetchall()

        groups_by_role: dict[str, set[str]] = {}
        users_by_role: dict[str, set[str]] = {}
        includes_by_role: dict[str, list[str]] = {}
        for r in group_rows:
            groups_by_role.setdefault(r["role_name"], set()).add(r["group_name"])
        for r in user_rows:
            users_by_role.setdefault(r["role_name"], set()).add(r["username_lower"])
        for r in include_rows:
            includes_by_role.setdefault(r["role_name"], []).append(r["included_role"])

        roles: dict[str, RoleDef] = {}
        for r in role_rows:
            name = r["name"]
            roles[name] = RoleDef(
                name=name,
                groups=frozenset(groups_by_role.get(name, set())),
                users_lower=frozenset(users_by_role.get(name, set())),
                includes=tuple(includes_by_role.get(name, [])),
            )

        closure = _compute_closure(roles)
        return RoleMapping(roles=roles, closure=closure)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._conn.close)
