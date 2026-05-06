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
from typing import TYPE_CHECKING

from iris.auth.authz.mapping import (
    ROLE_NAME_RE,
    RoleDef,
    RoleMapping,
    RoleMappingError,
    compute_closure,
)

if TYPE_CHECKING:
    from iris.auth.authz.bootstrap import BootstrapSettings


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
        # idempotent. synchronous + busy_timeout are connection-level.
        # Schema creation is deferred to bootstrap(); see the docstring on
        # bootstrap for why.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

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
            "SELECT role_name, included_role FROM authz_role_includes ORDER BY role_name, included_role"
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

        closure = compute_closure(roles)
        return RoleMapping(roles=roles, closure=closure)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._conn.close)

    def bootstrap(self, settings: "BootstrapSettings") -> None:
        """Create the schema (idempotent) and on first install seed the admin user.

        Why this isn't in __init__: bootstrap detects "first install" by
        checking whether `authz_roles` exists BEFORE running the schema
        statements. Creating the schema in __init__ would always make that
        check return True, so the seed branch would never fire. This method
        is the only place the schema is created.

        Called from iris.auth.routes.install at app construction; tests call
        it from their fixtures (with bootstrap_user=None to skip seeding).
        Synchronous because install runs eagerly before any request loop is
        active. With a :memory: DB the seed and the queries MUST share the
        same connection — each connection to ":memory:" is a private DB.
        """
        # Late import to avoid the circular bootstrap -> store -> bootstrap chain.
        from iris.auth.authz.bootstrap import install_authz_schema

        install_authz_schema(self._conn, settings)

    def _validate_role_name(self, name: str) -> None:
        if not ROLE_NAME_RE.fullmatch(name):
            raise RoleMappingError(f"invalid role name {name!r}")

    async def add_role(self, name: str) -> None:
        self._validate_role_name(name)
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "INSERT OR IGNORE INTO authz_roles(name) VALUES (?)",
                (name,),
            )

    async def remove_role(self, name: str) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(
                    self._conn.execute,
                    "DELETE FROM authz_roles WHERE name = ?",
                    (name,),
                )
            except sqlite3.IntegrityError as exc:
                raise RoleMappingError(
                    f"role {name!r} is included by other roles; remove the includes first"
                ) from exc

    async def add_group_to_role(self, role: str, group: str) -> None:
        async with self._lock:
            try:
                await asyncio.to_thread(
                    self._conn.execute,
                    "INSERT OR IGNORE INTO authz_role_groups(role_name, group_name) VALUES (?, ?)",
                    (role, group),
                )
            except sqlite3.IntegrityError as exc:
                raise RoleMappingError(
                    f"role {role!r} not defined; create it before assigning groups"
                ) from exc

    async def remove_group_from_role(self, role: str, group: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM authz_role_groups WHERE role_name = ? AND group_name = ?",
                (role, group),
            )

    async def add_user_to_role(self, role: str, username: str) -> None:
        username_lower = username.lower()
        async with self._lock:
            try:
                await asyncio.to_thread(
                    self._conn.execute,
                    "INSERT OR IGNORE INTO authz_role_users(role_name, username_lower) VALUES (?, ?)",
                    (role, username_lower),
                )
            except sqlite3.IntegrityError as exc:
                raise RoleMappingError(
                    f"role {role!r} not defined; create it before assigning users"
                ) from exc

    async def remove_user_from_role(self, role: str, username: str) -> None:
        username_lower = username.lower()
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM authz_role_users WHERE role_name = ? AND username_lower = ?",
                (role, username_lower),
            )

    async def add_include(self, role: str, included_role: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._add_include_sync, role, included_role)

    def _add_include_sync(self, role: str, included_role: str) -> None:
        # Both roles must exist (FKs would catch this, but we want clean errors).
        rows = self._conn.execute(
            "SELECT name FROM authz_roles WHERE name IN (?, ?)",
            (role, included_role),
        ).fetchall()
        existing = {r["name"] for r in rows}
        if role not in existing:
            raise RoleMappingError(f"role {role!r} not defined")
        if included_role not in existing:
            raise RoleMappingError(f"included role {included_role!r} not defined")

        # Cycle check: walk the existing graph plus the prospective new edge.
        # If we can reach `role` starting from `included_role`, the new edge
        # closes a cycle.
        edges_rows = self._conn.execute(
            "SELECT role_name, included_role FROM authz_role_includes"
        ).fetchall()
        adj: dict[str, list[str]] = {}
        for r in edges_rows:
            adj.setdefault(r["role_name"], []).append(r["included_role"])
        adj.setdefault(role, []).append(included_role)  # prospective edge

        visiting: set[str] = set()

        def reaches(start: str, current: str) -> bool:
            if current == start:
                return True
            if current in visiting:
                return False
            visiting.add(current)
            for nxt in adj.get(current, []):
                if reaches(start, nxt):
                    return True
            return False

        if reaches(role, included_role):
            raise RoleMappingError(
                f"cycle detected: {role!r} -> ... -> {role!r}"
            )

        self._conn.execute(
            "INSERT OR IGNORE INTO authz_role_includes(role_name, included_role) VALUES (?, ?)",
            (role, included_role),
        )

    async def remove_include(self, role: str, included_role: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM authz_role_includes WHERE role_name = ? AND included_role = ?",
                (role, included_role),
            )
