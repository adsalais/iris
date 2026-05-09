# Auth module reshape — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (the user's CLAUDE.md mandates Inline Execution over Subagent-Driven). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reshape `iris.auth` (split `identity.py`, rename `session.py`/`sessions.py`) and the `Rights` vocabulary (`Rights → Capabilities`, `UserSession → StoredSession`, `init_user_rights → provision_user`, `derive_rights → derive_capabilities`) so the type triad and file boundaries communicate their purpose by name. Bundles two cleanup riders (drop dead `logger`, drop spurious `async`).

**Architecture:** Pure mechanical rename + move refactor. **One atomic commit at the end.** No backwards-compat shims (per CLAUDE.md: "Big renames go through a deliberate breakage window with one big-bang commit at the end."). Intermediate states between tasks WILL be uncompilable; that is expected and the final verification gate (Task 18) catches every missed reference via `basedpyright --level warning`.

**Tech Stack:** Python 3.13, FastAPI 0.136, basedpyright, ruff, pytest 9, sqlite3 (stdlib), uv.

**Source spec:** `docs/superpowers/specs/2026-05-09-auth-module-reshape-design.md`.

---

## Task 1: Create feature branch and verify baseline

**Files:** none modified

- [ ] **Step 1.1: Create the feature branch**

```bash
git -C /home/driou/dev/project/iris checkout -b feature/auth-module-reshape
```

- [ ] **Step 1.2: Verify a clean baseline (the suite must be green BEFORE we start)**

Run:

```bash
uv run --project /home/driou/dev/project/iris ruff check
uv run --project /home/driou/dev/project/iris basedpyright --level warning
uv run --project /home/driou/dev/project/iris pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
```

Expected:
- ruff: zero warnings.
- basedpyright: zero errors, zero warnings.
- pytest: all unit tests pass. (Integration suites skipped — we run them in Task 18.)

If anything fails, stop. Fix or report before continuing — we cannot distinguish refactor breakage from pre-existing breakage.

- [ ] **Step 1.3: Capture the pre-refactor test inventory**

Run:

```bash
uv run --project /home/driou/dev/project/iris pytest --collect-only -q --ignore=tests/auth/integration --ignore=tests/clickhouse/integration > /tmp/pytest-inventory-before.txt
wc -l /tmp/pytest-inventory-before.txt
```

Save the line count. Task 18 will diff against this to confirm zero coverage regression.

- [ ] **Step 1.4: Do NOT commit**

This task is verification only.

---

## Task 2: Rename `auth/session.py` → `auth/rights.py` (Rights → Capabilities)

**Files:**
- Rename: `src/iris/auth/session.py` → `src/iris/auth/rights.py`
- Modify (post-rename): `src/iris/auth/rights.py` (full content rewrite)

- [ ] **Step 2.1: Rename the file**

```bash
git -C /home/driou/dev/project/iris mv src/iris/auth/session.py src/iris/auth/rights.py
```

- [ ] **Step 2.2: Replace the contents of `src/iris/auth/rights.py`**

Write the file to exactly this content:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Frozen view of a session's effective ClickHouse-derived authorization.

    Computed once at login by ``iris.clickhouse.capabilities.derive_capabilities`` and
    persisted on the session row. Routes never re-derive mid-session; operator
    changes take effect on the user's next login.
    """
    is_admin: bool
    can_create_database: bool
    db_admin: frozenset[str]
    db_writer: frozenset[str]
    db_reader: frozenset[str]

    def has_read(self, database: str) -> bool:
        return self.is_admin or database in (
            self.db_admin | self.db_writer | self.db_reader
        )

    def has_write(self, database: str) -> bool:
        return self.is_admin or database in (self.db_admin | self.db_writer)

    def has_admin(self, database: str) -> bool:
        return self.is_admin or database in self.db_admin


def capabilities_to_dict(c: Capabilities) -> dict[str, Any]:
    return {
        "is_admin": c.is_admin,
        "can_create_database": c.can_create_database,
        "db_admin": sorted(c.db_admin),
        "db_writer": sorted(c.db_writer),
        "db_reader": sorted(c.db_reader),
    }


def capabilities_from_dict(d: dict[str, Any]) -> Capabilities:
    return Capabilities(
        is_admin=bool(d.get("is_admin", False)),
        can_create_database=bool(d.get("can_create_database", False)),
        db_admin=frozenset(d.get("db_admin", [])),
        db_writer=frozenset(d.get("db_writer", [])),
        db_reader=frozenset(d.get("db_reader", [])),
    )


EMPTY_CAPABILITIES = Capabilities(
    is_admin=False,
    can_create_database=False,
    db_admin=frozenset(),
    db_writer=frozenset(),
    db_reader=frozenset(),
)
```

- [ ] **Step 2.3: Do NOT commit. Do NOT run tests yet** (importers haven't been updated).

---

## Task 3: Rename `auth/sessions.py` → `auth/store.py` (UserSession→StoredSession, Capabilities, set_capabilities, capabilities_json)

**Files:**
- Rename: `src/iris/auth/sessions.py` → `src/iris/auth/store.py`
- Modify (post-rename): `src/iris/auth/store.py` (full content rewrite)

- [ ] **Step 3.1: Rename the file**

```bash
git -C /home/driou/dev/project/iris mv src/iris/auth/sessions.py src/iris/auth/store.py
```

- [ ] **Step 3.2: Replace the contents of `src/iris/auth/store.py`**

Write the file to exactly this content:

```python
"""SQLite-backed session store.

One sqlite3.Connection per process. WAL mode + synchronous=NORMAL handle
cross-process locking so multiple uvicorn workers can share a single DB
file. All sync sqlite3 calls are wrapped in asyncio.to_thread to keep the
FastAPI event loop unblocked.

Schema:

    CREATE TABLE sessions (
        id                       TEXT PRIMARY KEY,
        subject                  TEXT NOT NULL,
        username                 TEXT NOT NULL,
        display_name             TEXT NOT NULL,
        groups_json              TEXT NOT NULL,
        created_at_ts            INTEGER NOT NULL,
        expires_at_ts            INTEGER NOT NULL,
        absolute_expires_at_ts   INTEGER NOT NULL,
        data_json                TEXT NOT NULL DEFAULT '{}',
        capabilities_json        TEXT NOT NULL DEFAULT '{}'
    );

Timestamps are Unix epoch INTEGER. Groups, data, and capabilities are JSON text.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, UTC
from typing import Any

from iris.auth.identity import StoredSession, User
from iris.auth.rights import (
    Capabilities,
    capabilities_from_dict,
    capabilities_to_dict,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                       TEXT PRIMARY KEY,
    subject                  TEXT NOT NULL,
    username                 TEXT NOT NULL,
    display_name             TEXT NOT NULL,
    groups_json              TEXT NOT NULL,
    created_at_ts            INTEGER NOT NULL,
    expires_at_ts            INTEGER NOT NULL,
    absolute_expires_at_ts   INTEGER NOT NULL,
    data_json                TEXT NOT NULL DEFAULT '{}',
    capabilities_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_sessions_subject ON sessions(subject);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at_ts);
"""


def _to_ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _from_ts(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def _row_to_session(row: sqlite3.Row) -> StoredSession:
    user = User(
        subject=row["subject"],
        username=row["username"],
        display_name=row["display_name"],
        groups=tuple(json.loads(row["groups_json"])),
    )
    capabilities = capabilities_from_dict(json.loads(row["capabilities_json"]))
    return StoredSession(
        id=row["id"],
        user=user,
        created_at=_from_ts(row["created_at_ts"]),
        expires_at=_from_ts(row["expires_at_ts"]),
        absolute_expires_at=_from_ts(row["absolute_expires_at_ts"]),
        data=json.loads(row["data_json"]),
        capabilities=capabilities,
    )


class SessionStore:
    def __init__(
        self,
        *,
        path: str,
        ttl_seconds: int,
        absolute_ttl_seconds: int,
        max_per_user: int = 10,
    ) -> None:
        """Open a SQLite-backed session store.

        Args:
            path: SQLite file path; ``":memory:"`` is supported for tests.
            ttl_seconds: sliding TTL refreshed on every ``get_and_refresh``.
            absolute_ttl_seconds: hard upper bound from ``created_at``;
                sessions past this expire even if recently refreshed.
            max_per_user: oldest sessions are pruned on ``create()`` once a
                subject exceeds this count.

        Concurrency: one ``sqlite3.Connection`` per process, serialized by
        ``self._lock`` (asyncio). Sync ``sqlite3`` calls run via
        ``asyncio.to_thread`` so the event loop stays unblocked. WAL mode
        plus ``synchronous=NORMAL`` make the file safe to share across
        multiple uvicorn workers.

        Lifecycle: ``close()`` is idempotent and required (registered into
        ``app.state.shutdown_hooks`` by ``iris.auth.routes.install``).
        """
        self._ttl = timedelta(seconds=ttl_seconds)
        self._absolute_ttl = timedelta(seconds=absolute_ttl_seconds)
        self._max_per_user = max_per_user
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
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    async def create(self, user: User) -> StoredSession:
        async with self._lock:
            return await asyncio.to_thread(self._create_sync, user)

    def _create_sync(self, user: User) -> StoredSession:
        now = datetime.now(UTC)
        session = StoredSession(
            id=secrets.token_urlsafe(32),
            user=user,
            created_at=now,
            expires_at=now + self._ttl,
            absolute_expires_at=now + self._absolute_ttl,
        )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                """
                INSERT INTO sessions (
                    id, subject, username, display_name, groups_json,
                    created_at_ts, expires_at_ts, absolute_expires_at_ts,
                    data_json, capabilities_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.user.subject,
                    session.user.username,
                    session.user.display_name,
                    json.dumps(list(session.user.groups)),
                    _to_ts(session.created_at),
                    _to_ts(session.expires_at),
                    _to_ts(session.absolute_expires_at),
                    "{}",
                    "{}",
                ),
            )
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE subject = ? ORDER BY created_at_ts ASC",
                (session.user.subject,),
            ).fetchall()
            excess = len(rows) - self._max_per_user
            if excess > 0:
                ids_to_delete = [r["id"] for r in rows[:excess]]
                self._conn.executemany(
                    "DELETE FROM sessions WHERE id = ?",
                    [(sid,) for sid in ids_to_delete],
                )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return session

    async def get_and_refresh(self, session_id: str) -> StoredSession | None:
        async with self._lock:
            return await asyncio.to_thread(self._get_and_refresh_sync, session_id)

    def _get_and_refresh_sync(self, session_id: str) -> StoredSession | None:
        # BEGIN IMMEDIATE acquires the write lock up front, so the
        # SELECT/UPDATE/DELETE window is atomic across processes that share
        # the WAL (multi-worker uvicorn). Mirrors _create_sync's pattern.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                self._conn.execute("COMMIT")
                return None
            now = datetime.now(UTC)
            expires_at = _from_ts(row["expires_at_ts"])
            absolute_expires_at = _from_ts(row["absolute_expires_at_ts"])
            if expires_at <= now or absolute_expires_at <= now:
                self._conn.execute(
                    "DELETE FROM sessions WHERE id = ?", (session_id,)
                )
                self._conn.execute("COMMIT")
                return None
            new_expires = now + self._ttl
            self._conn.execute(
                "UPDATE sessions SET expires_at_ts = ? WHERE id = ?",
                (_to_ts(new_expires), session_id),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        session = _row_to_session(row)
        return StoredSession(
            id=session.id,
            user=session.user,
            created_at=session.created_at,
            expires_at=new_expires,
            absolute_expires_at=session.absolute_expires_at,
            data=session.data,
            capabilities=session.capabilities,
        )

    async def update_data(self, session_id: str, data: dict[str, Any]) -> None:
        data_json = json.dumps(data)
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "UPDATE sessions SET data_json = ? WHERE id = ?",
                (data_json, session_id),
            )

    async def set_capabilities(
        self, session_id: str, capabilities: Capabilities
    ) -> None:
        """Persist the derived ``Capabilities`` view onto a session row.

        Called once per real login by the post-login hook chain after
        ``provision_user`` and ``derive_capabilities`` succeed.
        """
        capabilities_json = json.dumps(capabilities_to_dict(capabilities))
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "UPDATE sessions SET capabilities_json = ? WHERE id = ?",
                (capabilities_json, session_id),
            )

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._conn.execute,
                "DELETE FROM sessions WHERE id = ?",
                (session_id,),
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._conn.close)
```

- [ ] **Step 3.3: Do NOT commit. Do NOT run tests yet.**

---

## Task 4: Split `auth/identity.py` → `auth/identity.py` (User + StoredSession) + new `auth/views.py` (AuthSession family)

**Files:**
- Modify: `src/iris/auth/identity.py` (full content rewrite — trim to User + StoredSession; delete pyright suppression and TYPE_CHECKING block)
- Create: `src/iris/auth/views.py` (lift AuthSession + DatabaseSession family from old identity.py)

- [ ] **Step 4.1: Rewrite `src/iris/auth/identity.py` to contain only User + StoredSession**

Write the file to exactly this content:

```python
"""Identity dataclasses for the auth subsystem.

- ``User``: frozen, slotted, externally-derived identity (subject, username,
  display name, groups). Returned by every provider's ``authenticate``.
- ``StoredSession``: mutable row-shape persisted in the SQLite session store.
  The sliding-TTL refresh logic in ``iris.auth.store.SessionStore`` operates
  on this type. Routes never see ``StoredSession`` directly; they receive
  the request-scoped ``AuthSession`` view (and its subclasses) from
  ``iris.auth.views`` via the alias deps in ``iris.auth.deps``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities


@dataclass(frozen=True, slots=True)
class User:
    subject: str
    username: str
    display_name: str
    groups: tuple[str, ...]


@dataclass(slots=True)
class StoredSession:
    """Internal mutable session row from the SQLite store.

    Routes consume the request-scoped immutable :class:`AuthSession` view via
    the alias deps in ``iris.auth.deps``. ``StoredSession`` is the row shape
    that sliding-TTL refresh operates on.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    absolute_expires_at: datetime
    data: dict[str, Any] = field(default_factory=dict)
    capabilities: Capabilities = EMPTY_CAPABILITIES
```

- [ ] **Step 4.2: Create `src/iris/auth/views.py` with the AuthSession family**

Write the file to exactly this content:

```python
"""Request-scoped session views.

Each route receives an ``AuthSession`` (or a database-bound subclass) via the
``Annotated`` alias deps in ``iris.auth.deps``. These views carry the CH
client / httpx client / settings / SessionStore references that session
methods need to talk to ClickHouse; they are constructed once per request
and discarded at request end.

Frozen except for ``data``: the dict is a per-request snapshot deserialized
from the SQLite session store. Mutations to the dict do NOT auto-persist —
call ``await session.persist_data()`` to write the current ``data`` dict
back to the store before returning.

The ``client`` / ``http_client`` / ``settings`` / ``store`` fields are
``Optional`` because ``build_app(install_clickhouse=False)`` is a documented
test mode that wires up auth without ClickHouse. Subclass methods that
perform CH operations call ``self._ch()`` once at the top, which raises
if the refs are missing.

Note: ``AuthSession`` does not expose a ``query_as_user`` method. CH
impersonation requires a target database; the database-scoped subclasses
(``DatabaseSession`` and below) carry the per-database ``query_as_user``.
Admins query as the service identity via ``AdminSession.query_as_service``.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

import httpx
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.query import QueryResult

from iris.auth.identity import User
from iris.auth.rights import Capabilities
from iris.auth.store import SessionStore
from iris.clickhouse import audit, grants, policies
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    drop_tier_roles,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
    tier_role_name,
)
from iris.clickhouse.identifiers import quote_identifier, validate_identifier
from iris.clickhouse.queries import query_as_service, query_as_user
from iris.clickhouse.users import provision_user
from iris.clickhouse.config import ClickHouseSettings


@dataclass(frozen=True, slots=True)
class AuthSession:
    """Request-scoped view of a logged-in session.

    Built once per request by the auth dep. Routes receive an ``AuthSession``
    (or one of its subclasses: :class:`DatabaseSession`,
    :class:`DatabaseAdminSession`, :class:`DatabaseCreatorSession`,
    :class:`AdminSession`) via the ``Annotated`` alias deps in
    ``iris.auth.deps``.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    capabilities: Capabilities
    client: Client | None = field(repr=False, compare=False)
    http_client: httpx.AsyncClient | None = field(repr=False, compare=False)
    settings: ClickHouseSettings | None = field(repr=False, compare=False)
    store: SessionStore | None = field(repr=False, compare=False)

    async def persist_data(self) -> None:
        """Write the current ``data`` dict back to the session store.

        Routes that mutate ``session.data`` and want the change to survive the
        request call this before returning. Values must be JSON-encodable;
        anything else raises ``TypeError`` at write time.
        """
        if self.store is None:
            raise RuntimeError(
                "persist_data requires a SessionStore; this session was "
                + "constructed without one (typically a CH-only test fixture)"
            )
        await self.store.update_data(self.id, self.data)

    def _ch(self) -> tuple[Client, httpx.AsyncClient, ClickHouseSettings]:
        """Return the CH refs as a non-None triple, or raise if CH isn't installed.

        Subclasses that perform CH operations call this once at the top of
        each method instead of reading ``self.client`` / ``http_client`` /
        ``settings`` directly. The Optional fields exist to support
        ``build_app(install_clickhouse=False)`` — by the time a CH-using
        method runs, the alias deps have already gated on CH-derived
        ``Capabilities``, so the refs are populated in practice.
        """
        if (
            self.client is None
            or self.http_client is None
            or self.settings is None
        ):
            raise RuntimeError(
                "ClickHouse not installed; this method requires "
                + "build_app(install_clickhouse=True)"
            )
        return self.client, self.http_client, self.settings


@dataclass(frozen=True, slots=True)
class DatabaseSession(AuthSession):
    """Session bound to a specific database (the path/query parameter that
    drove the alias dep). ``query_as_user`` is auto-scoped to ``self.database``.
    To query a different database, use a fully-qualified table name and let
    CH enforce privileges.
    """
    database: str

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        _client, http_client, _settings = self._ch()
        return await query_as_user(
            http_client,
            username=self.user.username,
            sql=sql,
            parameters=parameters,
            database=self.database,
        )


@dataclass(frozen=True, slots=True)
class DatabaseAdminSession(DatabaseSession):
    """Per-database admin session. Adds tier-grant/revoke/lifecycle/audit
    methods scoped to ``self.database``."""

    async def grant_reader(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_user, client,
            database=self.database, tier=TIER_DBREADER, username=username,
        )

    async def grant_writer(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_user, client,
            database=self.database, tier=TIER_DBWRITER, username=username,
        )

    async def add_admin_user(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_user, client,
            database=self.database, tier=TIER_DBADMIN, username=username,
        )

    async def revoke_reader(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_user, client,
            database=self.database, tier=TIER_DBREADER, username=username,
        )

    async def revoke_writer(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_user, client,
            database=self.database, tier=TIER_DBWRITER, username=username,
        )

    async def remove_admin_user(self, username: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_user, client,
            database=self.database, tier=TIER_DBADMIN, username=username,
        )

    async def grant_reader_to_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_group, client,
            database=self.database, tier=TIER_DBREADER, group=group,
        )

    async def grant_writer_to_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_group, client,
            database=self.database, tier=TIER_DBWRITER, group=group,
        )

    async def add_admin_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grant_tier_to_group, client,
            database=self.database, tier=TIER_DBADMIN, group=group,
        )

    async def revoke_reader_from_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_group, client,
            database=self.database, tier=TIER_DBREADER, group=group,
        )

    async def revoke_writer_from_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_group, client,
            database=self.database, tier=TIER_DBWRITER, group=group,
        )

    async def remove_admin_group(self, group: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            revoke_tier_from_group, client,
            database=self.database, tier=TIER_DBADMIN, group=group,
        )

    async def delete_database(self) -> None:
        db_q = quote_identifier(self.database, kind="database")
        database = self.database
        client, _, _ = self._ch()

        def _sync() -> None:
            client.command(f"DROP DATABASE IF EXISTS {db_q}")
            drop_tier_roles(client, database=database)

        await asyncio.to_thread(_sync)

    async def list_admin_members(self) -> list[dict[str, str]]:
        """Return everything granted the per-database admin role.

        Each entry is ``{"kind": "user" | "role", "name": <str>}``. Includes
        direct user grantees AND role grantees (e.g. group-roles or
        per-user roles holding the admin tier).
        """
        admin_role = tier_role_name(self.database, TIER_DBADMIN)
        client, _, _ = self._ch()

        def _sync() -> list[dict[str, str]]:
            rows = client.query(
                """
                SELECT user_name, role_name FROM system.role_grants
                WHERE granted_role_name = {r:String}
                """,
                {"r": admin_role},
            )
            out: list[dict[str, str]] = []
            for row in rows.named_results():
                u = row.get("user_name")
                r = row.get("role_name")
                if u:
                    out.append({"kind": "user", "name": cast(str, u)})
                elif r:
                    out.append({"kind": "role", "name": cast(str, r)})
            return out

        return await asyncio.to_thread(_sync)

    async def list_grants(self) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        database = self.database

        def _sync() -> list[dict[str, Any]]:
            result = client.query(
                "SELECT * FROM system.grants WHERE database = {d:String}",
                parameters={"d": database},
            )
            return list(result.named_results())

        return await asyncio.to_thread(_sync)

    async def list_row_policies(self) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        database = self.database

        def _sync() -> list[dict[str, Any]]:
            result = client.query(
                "SELECT * FROM system.row_policies WHERE database = {d:String}",
                parameters={"d": database},
            )
            return list(result.named_results())

        return await asyncio.to_thread(_sync)


@dataclass(frozen=True, slots=True)
class DatabaseCreatorSession(AuthSession):
    """Session that can create new databases. Returned by the
    ``SessionDatabaseCreator`` alias when ``capabilities.is_admin`` or
    ``capabilities.can_create_database``."""

    async def create_database(self, name: str) -> None:
        validate_identifier(name, kind="database")
        quoted = quote_identifier(name, kind="database")
        creator_username = self.user.username
        client, _, _ = self._ch()

        def _sync() -> None:
            client.command(f"CREATE DATABASE IF NOT EXISTS {quoted}")
            create_tier_roles(client, database=name)
            grant_tier_to_user(
                client, database=name, tier=TIER_DBADMIN, username=creator_username,
            )

        await asyncio.to_thread(_sync)


@dataclass(frozen=True, slots=True)
class AdminSession(AuthSession):
    """Global-admin session. Adds service-identity queries plus audit and
    row-policy operations."""

    async def query_as_service(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> QueryResult:
        client, _, _ = self._ch()
        return await query_as_service(
            client, sql=sql, parameters=parameters, database=database,
        )

    async def reprovision_user(self, *, username: str, groups: list[str]) -> None:
        client, _, settings = self._ch()
        await asyncio.to_thread(
            provision_user, client,
            username=username, groups=groups, settings=settings,
        )

    async def grant_select_to_database(self, *, database: str, role: str) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grants.grant_select_to_database, client,
            database=database, role=role,
        )

    async def grant_insert_update_to_table(
        self, *, database: str, table: str, role: str
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            grants.grant_insert_update_to_table, client,
            database=database, table=table, role=role,
        )

    async def add_row_policy(
        self, *, database: str, table: str, column: str, role: str, value: str
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            policies.add_row_policy, client,
            database=database, table=table, column=column, role=role, value=value,
        )

    async def revoke_row_policy(
        self, *, database: str, table: str, role: str, value: str
    ) -> None:
        client, _, _ = self._ch()
        await asyncio.to_thread(
            policies.revoke_row_policy, client,
            database=database, table=table, role=role, value=value,
        )

    async def user_grants(self, *, username: str) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.user_grants, client, username=username)

    async def role_grants(self, *, role: str) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.role_grants, client, role=role)

    async def user_role_memberships(
        self, *, username: str
    ) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.user_role_memberships, client, username=username)

    async def user_row_policies(self, *, username: str) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.user_row_policies, client, username=username)

    async def role_row_policies(self, *, role: str) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(audit.role_row_policies, client, role=role)

    async def table_row_policies(
        self, *, database: str, table: str
    ) -> list[dict[str, Any]]:
        client, _, _ = self._ch()
        return await asyncio.to_thread(
            audit.table_row_policies, client,
            database=database, table=table,
        )
```

- [ ] **Step 4.3: Do NOT commit.**

Note: `views.py` imports `provision_user` from `iris.clickhouse.users` — we rename that function in Task 9. Until then, this file's imports point at a name that doesn't exist yet. Pyright will fail; that is expected.

---

## Task 5: Update `auth/deps.py` (imports + `.rights` → `.capabilities`)

**Files:**
- Modify: `src/iris/auth/deps.py` (full content rewrite)

- [ ] **Step 5.1: Replace the contents of `src/iris/auth/deps.py`**

Write the file to exactly this content:

```python
"""FastAPI dependency aliases for the CH-only authorization model.

Routes consume these as type annotations:

    @app.get("/me")
    async def me(session: Session) -> dict: ...

    @app.get("/db/{database}/read")
    async def read_db(database: str, session: SessionRead) -> ...: ...

Each alias resolves to a Session subclass whose method surface matches the
tier. Resolvers inject the ClickHouse client / httpx client / settings from
``request.app.state`` so session methods can talk to CH.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request

from iris.auth.exceptions import AuthForbidden, AuthRequired
from iris.auth.identity import StoredSession
from iris.auth.store import SessionStore
from iris.auth.views import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
)


def set_session_store(app: FastAPI, store: SessionStore) -> None:
    app.state.auth_session_store = store


def set_settings(app: FastAPI, *, cookie_name: str, cookie_secure: bool = True) -> None:
    app.state.auth_cookie_name = cookie_name
    app.state.auth_cookie_secure = cookie_secure


def _get_store(request: Request) -> SessionStore:
    return request.app.state.auth_session_store


def _get_cookie_name(request: Request) -> str:
    return request.app.state.auth_cookie_name


def _ch_refs(request: Request) -> tuple[Any, Any, Any]:
    """Return (clickhouse_client, http_client, settings) — or (None, None,
    None) when CH isn't installed (build_app(install_clickhouse=False)).
    Sessions constructed without CH refs raise on any attempt to call a CH
    method."""
    state = request.app.state
    return (
        getattr(state, "clickhouse_client", None),
        getattr(state, "clickhouse_http_client", None),
        getattr(state, "clickhouse_settings", None),
    )


async def _resolve_stored(request: Request) -> StoredSession | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name)
    if not sid:
        return None
    store = _get_store(request)
    return await store.get_and_refresh(sid)


_StoredSessionDep = Annotated[StoredSession | None, Depends(_resolve_stored)]


def _to_auth_session(stored: StoredSession, request: Request) -> AuthSession:
    client, http_client, settings = _ch_refs(request)
    return AuthSession(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        capabilities=stored.capabilities,
        client=client,
        http_client=http_client,
        settings=settings,
        store=_get_store(request),
    )


async def _optional_session(
    request: Request, stored: _StoredSessionDep
) -> AuthSession | None:
    if stored is None:
        return None
    return _to_auth_session(stored, request)


async def _require_session(
    request: Request, stored: _StoredSessionDep
) -> AuthSession:
    if stored is None:
        raise AuthRequired()
    return _to_auth_session(stored, request)


_RequiredAuth = Annotated[AuthSession, Depends(_require_session)]


async def _require_admin(session: _RequiredAuth) -> AdminSession:
    if not session.capabilities.is_admin:
        raise AuthForbidden(needed=("admin",), have=())
    return AdminSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        capabilities=session.capabilities,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        store=session.store,
    )


async def _require_database_creator(
    session: _RequiredAuth,
) -> DatabaseCreatorSession:
    c = session.capabilities
    if not (c.is_admin or c.can_create_database):
        raise AuthForbidden(needed=("admin", "database_creator"), have=())
    return DatabaseCreatorSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        capabilities=session.capabilities,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        store=session.store,
    )


async def _require_database_admin(
    database: str, session: _RequiredAuth
) -> DatabaseAdminSession:
    if not session.capabilities.has_admin(database):
        raise AuthForbidden(needed=(f"database_admin[{database}]",), have=())
    return DatabaseAdminSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        capabilities=session.capabilities,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        store=session.store,
        database=database,
    )


async def _require_write(
    database: str, session: _RequiredAuth
) -> DatabaseSession:
    if not session.capabilities.has_write(database):
        raise AuthForbidden(needed=(f"database_writer[{database}]",), have=())
    return DatabaseSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        capabilities=session.capabilities,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        store=session.store,
        database=database,
    )


async def _require_read(
    database: str, session: _RequiredAuth
) -> DatabaseSession:
    if not session.capabilities.has_read(database):
        raise AuthForbidden(needed=(f"database_reader[{database}]",), have=())
    return DatabaseSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        capabilities=session.capabilities,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        store=session.store,
        database=database,
    )


# Public Annotated aliases — what routes consume.
Session = Annotated[AuthSession, Depends(_require_session)]
SessionOptional = Annotated[AuthSession | None, Depends(_optional_session)]
SessionAdmin = Annotated[AdminSession, Depends(_require_admin)]
SessionDatabaseCreator = Annotated[
    DatabaseCreatorSession, Depends(_require_database_creator)
]
SessionDatabaseAdmin = Annotated[
    DatabaseAdminSession, Depends(_require_database_admin)
]
SessionWrite = Annotated[DatabaseSession, Depends(_require_write)]
SessionRead = Annotated[DatabaseSession, Depends(_require_read)]
```

Note: the local alias `_StoredSession` was renamed to `_StoredSessionDep` to avoid colliding with the new public type name `StoredSession` imported from `iris.auth.identity`. Public Annotated aliases (`Session`, `SessionRead`, etc.) keep their names per the spec's anti-list.

- [ ] **Step 5.2: Do NOT commit.**

---

## Task 6: Update `auth/routes.py` (imports + whoami `.rights` → `.capabilities`)

**Files:**
- Modify: `src/iris/auth/routes.py:19` (one import line)
- Modify: `src/iris/auth/routes.py:160-174` (whoami body)

- [ ] **Step 6.1: Update the SessionStore import**

Edit `src/iris/auth/routes.py`:

```
old: from iris.auth.sessions import SessionStore
new: from iris.auth.store import SessionStore
```

- [ ] **Step 6.2: Update the `whoami` route to read `session.capabilities`**

Edit `src/iris/auth/routes.py`:

```
old:
    @router.get("/api/whoami")
    async def whoami(session: Session) -> dict[str, Any]:
        r = session.rights
        return {
            "subject": session.user.subject,
            "display_name": session.user.display_name,
            "groups": list(session.user.groups),
            "rights": {
                "is_admin": r.is_admin,
                "can_create_database": r.can_create_database,
                "db_admin": sorted(r.db_admin),
                "db_writer": sorted(r.db_writer),
                "db_reader": sorted(r.db_reader),
            },
        }

new:
    @router.get("/api/whoami")
    async def whoami(session: Session) -> dict[str, Any]:
        c = session.capabilities
        return {
            "subject": session.user.subject,
            "display_name": session.user.display_name,
            "groups": list(session.user.groups),
            "capabilities": {
                "is_admin": c.is_admin,
                "can_create_database": c.can_create_database,
                "db_admin": sorted(c.db_admin),
                "db_writer": sorted(c.db_writer),
                "db_reader": sorted(c.db_reader),
            },
        }
```

Note: the JSON response key changed from `"rights"` to `"capabilities"`. Tests that scrape `/api/whoami` need this updated (handled in later tasks).

- [ ] **Step 6.3: Do NOT commit.**

---

## Task 7: Update `auth/__init__.py`, `auth/csrf.py` (drop `async`), `auth/exceptions.py` (drop `logger`)

**Files:**
- Modify: `src/iris/auth/__init__.py` (full rewrite)
- Modify: `src/iris/auth/csrf.py` (one keyword change)
- Modify: `src/iris/auth/exceptions.py` (delete one line)

- [ ] **Step 7.1: Replace the contents of `src/iris/auth/__init__.py`**

```python
from __future__ import annotations

from iris.auth.deps import (
    Session,
    SessionAdmin,
    SessionDatabaseAdmin,
    SessionDatabaseCreator,
    SessionOptional,
    SessionRead,
    SessionWrite,
)
from iris.auth.identity import User
from iris.auth.rights import EMPTY_CAPABILITIES, Capabilities
from iris.auth.routes import install
from iris.auth.views import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
)

__all__ = [
    "AdminSession",
    "AuthSession",
    "Capabilities",
    "DatabaseAdminSession",
    "DatabaseCreatorSession",
    "DatabaseSession",
    "EMPTY_CAPABILITIES",
    "Session",
    "SessionAdmin",
    "SessionDatabaseAdmin",
    "SessionDatabaseCreator",
    "SessionOptional",
    "SessionRead",
    "SessionWrite",
    "User",
    "install",
]
```

`StoredSession` is intentionally NOT re-exported (per the spec's "internal store-row type").

- [ ] **Step 7.2: Drop the `async` keyword on `verify_csrf_form` (U7)**

Edit `src/iris/auth/csrf.py`:

```
old: async def verify_csrf_form(
new: def verify_csrf_form(
```

The function does no `await`; FastAPI dispatches sync deps on its threadpool. Behavior is unchanged.

- [ ] **Step 7.3: Drop the unused logger declaration (U6)**

Edit `src/iris/auth/exceptions.py`:

```
old:
import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse

logger = logging.getLogger("iris.auth")

new:
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
```

The `logger` was never used. The `import logging` becomes unused too, so it goes.

- [ ] **Step 7.4: Do NOT commit.**

---

## Task 8: Rename `clickhouse/rights.py` → `clickhouse/capabilities.py` (`derive_rights` → `derive_capabilities`)

**Files:**
- Rename: `src/iris/clickhouse/rights.py` → `src/iris/clickhouse/capabilities.py`
- Modify (post-rename): `src/iris/clickhouse/capabilities.py` (full rewrite)

- [ ] **Step 8.1: Rename the file**

```bash
git -C /home/driou/dev/project/iris mv src/iris/clickhouse/rights.py src/iris/clickhouse/capabilities.py
```

- [ ] **Step 8.2: Replace the contents of `src/iris/clickhouse/capabilities.py`**

```python
"""Derive a session's effective Capabilities from ClickHouse RBAC at login.

Walks ``system.role_grants`` transitively for the user's effective role set
(``<username>_USER`` plus ``<group>_GRP`` for each group), then queries
``system.grants`` for the global flags. Returns a frozen ``Capabilities`` value.

Called by the post-login hook in ``iris.clickhouse.install`` exactly once per
real login. Operator changes to grants take effect on the user's next login.
"""
from __future__ import annotations

from typing import cast

from clickhouse_connect.driver.client import Client

from iris.auth.rights import Capabilities
from iris.clickhouse.grants import TIER_DBADMIN, TIER_DBREADER, TIER_DBWRITER
from iris.clickhouse.users import GROUP_ROLE_SUFFIX, USER_ROLE_SUFFIX

_TIER_SUFFIXES = (
    ("_" + TIER_DBADMIN, TIER_DBADMIN),
    ("_" + TIER_DBWRITER, TIER_DBWRITER),
    ("_" + TIER_DBREADER, TIER_DBREADER),
)


def _effective_role_set(
    client: Client, *, username: str, groups: list[str]
) -> set[str]:
    """Compute the transitive closure of role grants reachable from the user's
    seed roles (``<username>_USER`` plus each ``<group>_GRP``).

    ``system.role_grants.role_name`` is the parent that holds the grant;
    ``granted_role_name`` is the child role granted. The walk follows the
    parent→child edges starting from the seed set.
    """
    seed = {f"{username}{USER_ROLE_SUFFIX}"} | {
        f"{g}{GROUP_ROLE_SUFFIX}" for g in groups
    }
    closed: set[str] = set()
    frontier = set(seed)
    while frontier:
        closed |= frontier
        rows = client.query(
            """
            SELECT granted_role_name FROM system.role_grants
            WHERE role_name IN ({names:Array(String)})
            """,
            parameters={"names": list(frontier)},
        ).result_rows
        next_frontier = {cast(str, r[0]) for r in rows} - closed
        frontier = next_frontier
    return closed


def derive_capabilities(
    client: Client, *, username: str, groups: list[str]
) -> Capabilities:
    """Compute the user's ``Capabilities`` view from CH state.

    Pre-conditions: the user's per-user role and per-group roles must already
    exist in CH. Call after ``provision_user``.
    """
    effective = _effective_role_set(client, username=username, groups=groups)

    db_admin: set[str] = set()
    db_writer: set[str] = set()
    db_reader: set[str] = set()
    for role in effective:
        for suffix, tier in _TIER_SUFFIXES:
            if role.endswith(suffix):
                database = role[: -len(suffix)]
                if not database:
                    continue
                if tier == TIER_DBADMIN:
                    db_admin.add(database)
                elif tier == TIER_DBWRITER:
                    db_writer.add(database)
                else:
                    db_reader.add(database)
                break

    is_admin = False
    can_create_database = False
    if effective:
        # Check system.grants for the admin / database-creation markers.
        # Global scope is `database IS NULL` (CH uses NULL, not '', for
        # "no scope").
        #
        # CH usually stores grants in expanded form — individual access
        # types per row. But when the granter holds the FULL privilege
        # set (i.e. GRANT ALL or CURRENT GRANTS from a true superuser),
        # CH condenses to a single `access_type='ALL'` row. So we accept
        # either the expanded ROLE ADMIN row or the condensed ALL row as
        # the admin marker.
        rows = client.query(
            """
            SELECT DISTINCT access_type, grant_option
            FROM system.grants
            WHERE role_name IN ({names:Array(String)})
              AND database IS NULL
              AND access_type IN ('ALL', 'ROLE ADMIN', 'CREATE DATABASE')
            """,
            parameters={"names": list(effective)},
        ).result_rows
        for access_type, grant_option in rows:
            access_type = cast(str, access_type)
            grant_option_v = cast(int, grant_option)
            if access_type == "ALL":
                if grant_option_v == 1:
                    is_admin = True
                can_create_database = True
            elif access_type == "ROLE ADMIN" and grant_option_v == 1:
                is_admin = True
            elif access_type == "CREATE DATABASE":
                can_create_database = True

    return Capabilities(
        is_admin=is_admin,
        can_create_database=can_create_database,
        db_admin=frozenset(db_admin),
        db_writer=frozenset(db_writer),
        db_reader=frozenset(db_reader),
    )
```

- [ ] **Step 8.3: Do NOT commit.**

---

## Task 9: Rename `init_user_rights` → `provision_user` in `clickhouse/users.py`

**Files:**
- Modify: `src/iris/clickhouse/users.py:16` (function rename + docstring update)

- [ ] **Step 9.1: Rename the function**

Edit `src/iris/clickhouse/users.py`:

```
old:
def init_user_rights(
    client: Client,
    *,
    username: str,
    groups: list[str],
    settings: ClickHouseSettings,
) -> None:
    """Idempotently provision a CH user, their per-user role, group memberships, and the
    IMPERSONATE grant for the service admin.

    The per-user role (``<username>_USER``) is granted unconditionally and is *not*
    part of the group reconcile — it represents the user's own identity, distinct
    from group membership.
    """

new:
def provision_user(
    client: Client,
    *,
    username: str,
    groups: list[str],
    settings: ClickHouseSettings,
) -> None:
    """Idempotently provision a CH user, their per-user role, group memberships, and the
    IMPERSONATE grant for the service admin.

    The per-user role (``<username>_USER``) is granted unconditionally and is *not*
    part of the group reconcile — it represents the user's own identity, distinct
    from group membership.
    """
```

The function body is unchanged.

- [ ] **Step 9.2: Do NOT commit.**

---

## Task 10: Update `clickhouse/install.py` (imports + log line)

**Files:**
- Modify: `src/iris/clickhouse/install.py:9` (docstring)
- Modify: `src/iris/clickhouse/install.py:23-28` (imports)
- Modify: `src/iris/clickhouse/install.py:62-90` (`_provision_on_login` body)

- [ ] **Step 10.1: Update the module docstring**

Edit `src/iris/clickhouse/install.py`:

```
old: post-login provisioning hook so init_user_rights + derive_rights run once
new: post-login provisioning hook so provision_user + derive_capabilities run once
```

- [ ] **Step 10.2: Update the imports**

Edit `src/iris/clickhouse/install.py`:

```
old:
from iris.auth.identity import User
from iris.auth.sessions import SessionStore
from iris.clickhouse.bootstrap import bootstrap_admin
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

new:
from iris.auth.identity import User
from iris.auth.store import SessionStore
from iris.clickhouse.bootstrap import bootstrap_admin
from iris.clickhouse.capabilities import derive_capabilities
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.users import provision_user
```

- [ ] **Step 10.3: Update the `_provision_on_login` body**

Edit `src/iris/clickhouse/install.py`:

```
old:
    async def _provision_on_login(user: User, session_id: str) -> None:
        await asyncio.to_thread(
            init_user_rights,
            client,
            username=user.username,
            groups=list(user.groups),
            settings=settings,
        )
        rights = await asyncio.to_thread(
            derive_rights,
            client,
            username=user.username,
            groups=list(user.groups),
        )
        store: SessionStore = app.state.auth_session_store
        await store.set_rights(session_id, rights)
        logger.info(
            (
                "clickhouse: provisioned username=%s groups=%s "
                "rights=admin:%s creator:%s reader:%d writer:%d db_admin:%d"
            ),
            user.username,
            list(user.groups),
            rights.is_admin,
            rights.can_create_database,
            len(rights.db_reader),
            len(rights.db_writer),
            len(rights.db_admin),
        )

new:
    async def _provision_on_login(user: User, session_id: str) -> None:
        await asyncio.to_thread(
            provision_user,
            client,
            username=user.username,
            groups=list(user.groups),
            settings=settings,
        )
        capabilities = await asyncio.to_thread(
            derive_capabilities,
            client,
            username=user.username,
            groups=list(user.groups),
        )
        store: SessionStore = app.state.auth_session_store
        await store.set_capabilities(session_id, capabilities)
        logger.info(
            (
                "clickhouse: provisioned username=%s groups=%s "
                "capabilities=admin:%s creator:%s reader:%d writer:%d db_admin:%d"
            ),
            user.username,
            list(user.groups),
            capabilities.is_admin,
            capabilities.can_create_database,
            len(capabilities.db_reader),
            len(capabilities.db_writer),
            len(capabilities.db_admin),
        )
```

- [ ] **Step 10.4: Do NOT commit.**

---

## Task 11: Update `clickhouse/__init__.py` re-exports

**Files:**
- Modify: `src/iris/clickhouse/__init__.py` (full rewrite)

- [ ] **Step 11.1: Replace the contents of `src/iris/clickhouse/__init__.py`**

```python
"""ClickHouse provisioning, audit helpers, and per-tier ops.

Public surface — see ``CLAUDE.md`` for usage. Session subclasses in
``iris.auth.views`` call into these helpers via ``asyncio.to_thread``.

The ``install`` function lives in ``iris.clickhouse.install`` but is *not*
re-exported from this package: callers (only ``iris.app:build_app``) do
``from iris.clickhouse.install import install``. Removing it from this
``__init__`` breaks an old module-load cycle where importing the package
triggered loading ``iris.auth.bootstrap`` via ``install``.
"""
from __future__ import annotations

from iris.clickhouse.audit import (
    role_grants,
    role_row_policies,
    table_row_policies,
    user_grants,
    user_role_memberships,
    user_row_policies,
)
from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE, bootstrap_admin
from iris.clickhouse.capabilities import derive_capabilities
from iris.clickhouse.client import build_client
from iris.clickhouse.config import ClickHouseSettings
from iris.clickhouse.grants import (
    TIER_DBADMIN,
    TIER_DBREADER,
    TIER_DBWRITER,
    create_tier_roles,
    drop_tier_roles,
    grant_insert_update_to_table,
    grant_select_to_database,
    grant_tier_to_group,
    grant_tier_to_user,
    revoke_tier_from_group,
    revoke_tier_from_user,
    tier_role_name,
)
from iris.clickhouse.policies import add_row_policy, revoke_row_policy
from iris.clickhouse.users import provision_user

__all__ = [
    "ClickHouseSettings",
    "GLOBAL_ADMIN_ROLE",
    "TIER_DBADMIN",
    "TIER_DBREADER",
    "TIER_DBWRITER",
    "add_row_policy",
    "bootstrap_admin",
    "build_client",
    "create_tier_roles",
    "derive_capabilities",
    "drop_tier_roles",
    "grant_insert_update_to_table",
    "grant_select_to_database",
    "grant_tier_to_group",
    "grant_tier_to_user",
    "provision_user",
    "revoke_row_policy",
    "revoke_tier_from_group",
    "revoke_tier_from_user",
    "role_grants",
    "role_row_policies",
    "table_row_policies",
    "tier_role_name",
    "user_grants",
    "user_role_memberships",
    "user_row_policies",
]
```

- [ ] **Step 11.2: Do NOT commit.**

---

## Task 12: Rename `tests/auth/test_rights.py` → `tests/auth/test_capabilities.py`

**Files:**
- Rename: `tests/auth/test_rights.py` → `tests/auth/test_capabilities.py`
- Modify: full content rewrite

- [ ] **Step 12.1: Rename the file**

```bash
git -C /home/driou/dev/project/iris mv tests/auth/test_rights.py tests/auth/test_capabilities.py
```

- [ ] **Step 12.2: Rewrite the contents of `tests/auth/test_capabilities.py`**

Read the existing file first to preserve every test's behavior, then rewrite. The expected new contents (the tests cover constructors, the three `has_*` predicates, and the dict round-trip):

```python
from iris.auth.rights import Capabilities, capabilities_from_dict, capabilities_to_dict


def test_has_read_when_user_has_read_role():
    r = Capabilities(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset({"sales"}),
    )
    assert r.has_read("sales") is True


def test_has_read_when_user_has_writer_role():
    r = Capabilities(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset({"sales"}),
        db_reader=frozenset(),
    )
    assert r.has_read("sales") is True


def test_has_read_when_user_has_admin_role():
    r = Capabilities(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset({"sales"}),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert r.has_read("sales") is True


def test_has_write_when_user_has_only_reader_role():
    r = Capabilities(
        is_admin=False,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset({"sales"}),
    )
    assert r.has_write("sales") is False


def test_has_admin_when_user_is_global_admin():
    r = Capabilities(
        is_admin=True,
        can_create_database=False,
        db_admin=frozenset(),
        db_writer=frozenset(),
        db_reader=frozenset(),
    )
    assert r.has_admin("anything") is True


def test_round_trip_through_dict_preserves_value():
    r = Capabilities(
        is_admin=False,
        can_create_database=True,
        db_admin=frozenset({"sales"}),
        db_writer=frozenset({"marketing"}),
        db_reader=frozenset({"finance"}),
    )
    d = capabilities_to_dict(r)
    assert d == {
        "is_admin": False,
        "can_create_database": True,
        "db_admin": ["sales"],
        "db_writer": ["marketing"],
        "db_reader": ["finance"],
    }
    assert capabilities_from_dict(d) == r


def test_capabilities_from_dict_with_missing_fields_uses_empty_defaults():
    r = capabilities_from_dict({"is_admin": False, "can_create_database": False})
    assert r.db_admin == frozenset()
    assert r.db_writer == frozenset()
    assert r.db_reader == frozenset()
```

**Important:** Before pasting this content, READ the existing `tests/auth/test_rights.py` and confirm the test names + assertions match. If the existing file has additional tests not listed above, add them to the rewrite (translating `Rights` → `Capabilities`, `rights_*` → `capabilities_*`).

- [ ] **Step 12.3: Do NOT commit.**

---

## Task 13: Rename `tests/clickhouse/test_rights_derivation.py` → `tests/clickhouse/test_capabilities_derivation.py`

**Files:**
- Rename: `tests/clickhouse/test_rights_derivation.py` → `tests/clickhouse/test_capabilities_derivation.py`
- Modify: full file (all references)

- [ ] **Step 13.1: Rename the file**

```bash
git -C /home/driou/dev/project/iris mv tests/clickhouse/test_rights_derivation.py tests/clickhouse/test_capabilities_derivation.py
```

- [ ] **Step 13.2: Replace renamed symbols in the file**

The existing file imports `EMPTY_RIGHTS`, `derive_rights`, `init_user_rights` and uses `derive_rights(...)` and `init_user_rights(...)` calls plus `EMPTY_RIGHTS` literal. Apply these substitutions throughout the file:

```
EMPTY_RIGHTS  → EMPTY_CAPABILITIES
derive_rights → derive_capabilities
init_user_rights → provision_user
iris.auth.session  → iris.auth.rights
iris.clickhouse.rights  → iris.clickhouse.capabilities
iris.clickhouse.users import init_user_rights → iris.clickhouse.users import provision_user
```

Concretely, rewrite the import block:

```
old:
from iris.auth.session import EMPTY_RIGHTS
...
from iris.clickhouse.rights import derive_rights
from iris.clickhouse.users import init_user_rights

new:
from iris.auth.rights import EMPTY_CAPABILITIES
...
from iris.clickhouse.capabilities import derive_capabilities
from iris.clickhouse.users import provision_user
```

And replace every body call:

```
init_user_rights(  →  provision_user(
derive_rights(     →  derive_capabilities(
EMPTY_RIGHTS       →  EMPTY_CAPABILITIES
```

Use `sed`-style verification:

```bash
grep -n "init_user_rights\|derive_rights\|EMPTY_RIGHTS\|iris.auth.session\|iris.clickhouse.rights" tests/clickhouse/test_capabilities_derivation.py
```

Expected: zero matches.

- [ ] **Step 13.3: Do NOT commit.**

---

## Task 14: Update remaining `tests/auth/` files

**Files (all under `/home/driou/dev/project/iris/`):**
- Modify: `tests/auth/test_session_dep.py`
- Modify: `tests/auth/test_session_store.py`
- Modify: `tests/auth/test_session_store_multiprocess.py`
- Modify: `tests/auth/test_provider_oauth.py`
- Modify: `tests/auth/test_error_pages.py`
- Modify: `tests/auth/integration/test_oauth_integration.py`

For every file in the list, apply these substitutions consistently:

| Old | New |
|---|---|
| `from iris.auth.session import EMPTY_RIGHTS` | `from iris.auth.rights import EMPTY_CAPABILITIES` |
| `from iris.auth.session import Rights` | `from iris.auth.rights import Capabilities` |
| `from iris.auth.session import Rights, ...` | `from iris.auth.rights import Capabilities, ...` (preserve other imports, translating each) |
| `from iris.auth.sessions import SessionStore` | `from iris.auth.store import SessionStore` |
| `from iris.auth.sessions import UserSession` | `from iris.auth.identity import StoredSession` |
| `EMPTY_RIGHTS` (literal) | `EMPTY_CAPABILITIES` |
| `Rights(` (constructor call) | `Capabilities(` |
| `rights_to_dict(` | `capabilities_to_dict(` |
| `rights_from_dict(` | `capabilities_from_dict(` |
| `UserSession` (type ref) | `StoredSession` |
| `.rights.` (field access) | `.capabilities.` |
| `rights=` (kwarg in dataclass construction or seed helpers) | `capabilities=` |
| `set_rights(` | `set_capabilities(` |
| `"rights"` (JSON-key string in /api/whoami responses) | `"capabilities"` |
| references in docstrings to `init_user_rights` | `provision_user` |
| references in docstrings to `derive_rights` | `derive_capabilities` |
| references in docstrings to `Rights` (type name) | `Capabilities` |
| references in docstrings to `UserSession` | `StoredSession` |

- [ ] **Step 14.1: Update `tests/auth/test_session_dep.py`**

This file has the most occurrences (Rights constructor, kwarg `rights=`, `.rights.is_admin` etc., test name `test_rights_round_trip_through_set_rights`, `set_rights(`, etc.).

Apply ALL substitutions from the table above. Additionally, rename the test function:

```
old: def test_rights_round_trip_through_set_rights(tmp_path):
new: def test_capabilities_round_trip_through_set_capabilities(tmp_path):
```

Verify completion:

```bash
grep -n "Rights\|rights=\|\.rights\b\|set_rights\|EMPTY_RIGHTS\|UserSession\|iris.auth.session\|iris.auth.sessions" tests/auth/test_session_dep.py
```

Expected: zero matches.

- [ ] **Step 14.2: Update `tests/auth/test_session_store.py`**

```
old: from iris.auth.sessions import SessionStore
new: from iris.auth.store import SessionStore
```

Then verify:

```bash
grep -n "Rights\|rights=\|\.rights\b\|set_rights\|EMPTY_RIGHTS\|UserSession\|iris.auth.session\|iris.auth.sessions" tests/auth/test_session_store.py
```

Expected: zero matches.

- [ ] **Step 14.3: Update `tests/auth/test_session_store_multiprocess.py`**

```
old: from iris.auth.sessions import SessionStore
new: from iris.auth.store import SessionStore
```

Then verify the same grep returns zero. If it shows additional `rights_json` or `capabilities_json` SQL references, replace `rights_json` → `capabilities_json`.

- [ ] **Step 14.4: Update `tests/auth/test_provider_oauth.py`**

```
old: from iris.auth.sessions import SessionStore
new: from iris.auth.store import SessionStore
```

Then verify the same grep returns zero matches (allowing `iris.clickhouse.rights` if any test still mentions the old module — also rename to `iris.clickhouse.capabilities`).

- [ ] **Step 14.5: Update `tests/auth/test_error_pages.py`**

The references are inside comments only:

```
old: # tests run with install_clickhouse=False so derive_rights never runs —
old: # bob's session has empty Rights. The SessionAdmin-gated route 403s.

new: # tests run with install_clickhouse=False so derive_capabilities never runs —
new: # bob's session has empty Capabilities. The SessionAdmin-gated route 403s.
```

- [ ] **Step 14.6: Update `tests/auth/integration/test_oauth_integration.py`**

The reference is in a comment:

```
old: rights derivation — alice's session lands with EMPTY_RIGHTS. The
new: capabilities derivation — alice's session lands with EMPTY_CAPABILITIES. The
```

- [ ] **Step 14.7: Verify the entire `tests/auth/` tree is clean**

```bash
grep -rn "Rights\|rights=\|\.rights\b\|set_rights\|EMPTY_RIGHTS\|UserSession\|iris\.auth\.session\b\|iris\.auth\.sessions\b\|iris\.clickhouse\.rights\b\|init_user_rights\|derive_rights" tests/auth/
```

Expected: zero matches. If anything appears, either fix it now or document it as a follow-up — the final pyright gate must catch it.

- [ ] **Step 14.8: Do NOT commit.**

---

## Task 15: Update remaining `tests/clickhouse/` files (non-integration)

**Files (all under `/home/driou/dev/project/iris/`):**
- Modify: `tests/clickhouse/test_admin_handle.py`
- Modify: `tests/clickhouse/test_bootstrap_admin.py`
- Modify: `tests/clickhouse/test_clickhouse_audit.py`
- Modify: `tests/clickhouse/test_clickhouse_identifiers.py`
- Modify: `tests/clickhouse/test_clickhouse_users.py`
- Modify: `tests/clickhouse/test_creator_handle.py`
- Modify: `tests/clickhouse/test_handle_integration.py`
- Modify: `tests/clickhouse/test_install.py`
- Modify: `tests/clickhouse/test_login_provisioning.py`
- Modify: `tests/clickhouse/test_tier_promotion.py`
- Modify: `tests/clickhouse/conftest.py`

Apply the substitution table from Task 14 to every file. Plus, in `test_clickhouse_identifiers.py` specifically, the strings `"derive_rights"` and `"init_user_rights"` inside the `expected` set MUST be replaced by `"derive_capabilities"` and `"provision_user"` to match the new `iris.clickhouse.__all__`.

- [ ] **Step 15.1: Update `tests/clickhouse/test_admin_handle.py`**

```
from iris.auth.session import EMPTY_RIGHTS  →  from iris.auth.rights import EMPTY_CAPABILITIES
from iris.clickhouse.rights import derive_rights  →  from iris.clickhouse.capabilities import derive_capabilities
EMPTY_RIGHTS (literal occurrences)  →  EMPTY_CAPABILITIES
derive_rights(  →  derive_capabilities(
rights= (kwarg)  →  capabilities=
```

- [ ] **Step 15.2: Update `tests/clickhouse/test_bootstrap_admin.py`**

```
from iris.clickhouse.rights import derive_rights  →  from iris.clickhouse.capabilities import derive_capabilities
derive_rights(  →  derive_capabilities(
```

- [ ] **Step 15.3: Update `tests/clickhouse/test_clickhouse_audit.py`**

```
from iris.clickhouse.users import init_user_rights  →  from iris.clickhouse.users import provision_user
init_user_rights(  →  provision_user(
```

- [ ] **Step 15.4: Update `tests/clickhouse/test_clickhouse_identifiers.py`**

This file asserts on `iris.clickhouse.__all__`. Update the literal strings:

```
old:
        "derive_rights",
        ...
        "init_user_rights",

new:
        "derive_capabilities",
        ...
        "provision_user",
```

(The expected set is sorted alphabetically — `derive_capabilities` sorts BEFORE `derive_rights` did, and `provision_user` sorts AFTER `init_user_rights` did. Verify alphabetical order in the rewritten file.)

- [ ] **Step 15.5: Update `tests/clickhouse/test_clickhouse_users.py`**

This file is dense with `init_user_rights` references — both the imports, the function calls, and a leading docstring "Tests for init_user_rights". Apply substitutions:

```
"""Tests for init_user_rights — staged across Tasks 11/12/13."""
                ↓
"""Tests for provision_user — staged across Tasks 11/12/13."""

from iris.clickhouse.users import init_user_rights, ...
                ↓
from iris.clickhouse.users import provision_user, ...

init_user_rights(  →  provision_user(
```

Then rename these eight test functions (the full list — verify in the file with `grep -n "def test_init_user_rights" tests/clickhouse/test_clickhouse_users.py`):

| Before | After |
|---|---|
| `test_init_user_rights_creates_user_and_per_user_role` | `test_provision_user_creates_user_and_per_user_role` |
| `test_init_user_rights_is_idempotent` | `test_provision_user_is_idempotent` |
| `test_init_user_rights_rejects_bad_username` | `test_provision_user_rejects_bad_username` |
| `test_init_user_rights_rejects_bad_group` | `test_provision_user_rejects_bad_group` |
| `test_init_user_rights_grants_group_roles` | `test_provision_user_grants_group_roles` |
| `test_init_user_rights_revokes_groups_user_no_longer_has` | `test_provision_user_revokes_groups_user_no_longer_has` |
| `test_init_user_rights_does_not_touch_user_role_during_reconcile` | `test_provision_user_does_not_touch_user_role_during_reconcile` |
| `test_init_user_rights_grants_impersonate_to_connection_user` | `test_provision_user_grants_impersonate_to_connection_user` |

- [ ] **Step 15.6: Update `tests/clickhouse/test_creator_handle.py`**

```
from iris.auth.session import EMPTY_RIGHTS  →  from iris.auth.rights import EMPTY_CAPABILITIES
EMPTY_RIGHTS  →  EMPTY_CAPABILITIES
rights= (kwarg)  →  capabilities=
```

- [ ] **Step 15.7: Update `tests/clickhouse/test_handle_integration.py`**

```
from iris.clickhouse.users import init_user_rights  →  from iris.clickhouse.users import provision_user
init_user_rights(  →  provision_user(
```

- [ ] **Step 15.8: Update `tests/clickhouse/test_install.py`**

This file has docstring + import + `.rights` references. Apply the full Task-14 substitution table.

```
from iris.auth.sessions import SessionStore  →  from iris.auth.store import SessionStore
"The hook now does two things per login: init_user_rights (CH user/role"
                ↓
"The hook now does two things per login: provision_user (CH user/role"
"provisioning) and derive_rights (cache the Rights view on the session row)."
                ↓
"provisioning) and derive_capabilities (cache the Capabilities view on the session row)."
"Rights row to the session store"
                ↓
"Capabilities row to the session store"
.rights.db_admin  →  .capabilities.db_admin
.rights.db_writer  →  .capabilities.db_writer
.rights.db_reader  →  .capabilities.db_reader
```

- [ ] **Step 15.9: Update `tests/clickhouse/test_login_provisioning.py`**

```
"""Bridge tests: form-login through TestClient triggers init_user_rights."""
                ↓
"""Bridge tests: form-login through TestClient triggers provision_user."""
```

(Plus any other body references — apply the substitution table.)

- [ ] **Step 15.10: Update `tests/clickhouse/test_tier_promotion.py`**

```
from iris.auth.session import EMPTY_RIGHTS  →  from iris.auth.rights import EMPTY_CAPABILITIES
from iris.clickhouse.rights import derive_rights  →  from iris.clickhouse.capabilities import derive_capabilities
from iris.clickhouse.users import init_user_rights  →  from iris.clickhouse.users import provision_user
EMPTY_RIGHTS  →  EMPTY_CAPABILITIES
derive_rights(  →  derive_capabilities(
init_user_rights(  →  provision_user(
rights= (kwarg in dataclass construction)  →  capabilities=
bob_rights_before / bob_rights_after  →  bob_caps_before / bob_caps_after  (local variable rename for clarity)
rights = derive_rights(...)  →  capabilities = derive_capabilities(...)  (any local var `rights` renamed to `capabilities`)
```

- [ ] **Step 15.11: Update `tests/clickhouse/conftest.py`**

References are in comments only:

```
"# Allow the svc user to create SQL-managed users (needed by init_user_rights)."
                ↓
"# Allow the svc user to create SQL-managed users (needed by provision_user)."
"# init_user_rights.  Note: when a wildcard IMPERSONATE grant already"
                ↓
"# provision_user.  Note: when a wildcard IMPERSONATE grant already"
```

- [ ] **Step 15.12: Verify the tests/clickhouse/ tree (excluding integration/) is clean**

```bash
grep -rn "Rights\|rights=\|\.rights\b\|set_rights\|EMPTY_RIGHTS\|UserSession\|iris\.auth\.session\b\|iris\.auth\.sessions\b\|iris\.clickhouse\.rights\b\|init_user_rights\|derive_rights" tests/clickhouse/ --exclude-dir=integration
```

Expected: zero matches.

- [ ] **Step 15.13: Do NOT commit.**

---

## Task 16: Update `tests/clickhouse/integration/` files

**Files:**
- Modify: `tests/clickhouse/integration/_helpers.py`
- Modify: `tests/clickhouse/integration/test_creator_flow.py`
- Modify: `tests/clickhouse/integration/test_revoke_flow.py`

- [ ] **Step 16.1: Update `tests/clickhouse/integration/_helpers.py`**

This is the densest test file: imports, docstrings, type aliases, function bodies, kwargs, AND local variables named `rights`. Apply the full Task-14 substitution table, AND explicitly rename every local variable `rights` to `capabilities`:

```
from iris.clickhouse.rights import derive_rights  →  from iris.clickhouse.capabilities import derive_capabilities
"""...stored ``UserSession``..."""  →  """...stored ``StoredSession``..."""
"""Reconstitute a typed Session subclass from the stored UserSession."""  →  """Reconstitute a typed Session subclass from the stored StoredSession."""

# Local-variable rename: every `rights` local in this file becomes `capabilities`.
rights = derive_rights(...)  →  capabilities = derive_capabilities(...)         # line ~95
await store.set_rights(sid, rights)  →  await store.set_capabilities(sid, capabilities)   # line ~100
rights = stored.rights  →  capabilities = stored.capabilities                  # line ~123
data=stored.data, rights=rights,  →  data=stored.data, capabilities=capabilities,   # lines ~129, ~139, ~151, ~164, ~177, ~190 — SIX call sites
```

After the local-variable rename, the kwarg name in each `Session*Session(...)` constructor call also flips: `rights=rights` → `capabilities=capabilities` (kwarg uses the new field name; value uses the new local name). Both halves of every assignment must change in lockstep.

Verification:

```bash
grep -n "\brights\b\|set_rights\|derive_rights\|UserSession\|iris\.clickhouse\.rights" tests/clickhouse/integration/_helpers.py
```

Expected: zero matches.

- [ ] **Step 16.2: Update `tests/clickhouse/integration/test_creator_flow.py`**

```
old: assert creator.rights.can_create_database is True
new: assert creator.capabilities.can_create_database is True
```

- [ ] **Step 16.3: Update `tests/clickhouse/integration/test_revoke_flow.py`**

```
old: assert db in carol_first.rights.db_writer
new: assert db in carol_first.capabilities.db_writer
```

- [ ] **Step 16.4: Verify integration tests are clean**

```bash
grep -rn "Rights\|rights=\|\.rights\b\|set_rights\|EMPTY_RIGHTS\|UserSession\|iris\.auth\.session\b\|iris\.auth\.sessions\b\|iris\.clickhouse\.rights\b\|init_user_rights\|derive_rights" tests/clickhouse/integration/ tests/auth/integration/
```

Expected: zero matches.

- [ ] **Step 16.5: Do NOT commit.**

---

## Task 17: Update `CLAUDE.md` and `docs/` (auth.md, clickhouse.md, operations.md)

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/auth.md`
- Modify: `docs/clickhouse.md`
- Modify: `docs/operations.md`

- [ ] **Step 17.1: Update `CLAUDE.md`**

The file references `iris.clickhouse.{audit,grants,policies,users,queries}` in the Conventions section, the auth module map, and the Rights/Session terminology. Search for renamed symbols and apply substitutions:

```bash
grep -n "Rights\|UserSession\|init_user_rights\|derive_rights\|set_rights\|EMPTY_RIGHTS\|iris\.auth\.session\b\|iris\.auth\.sessions\b\|iris\.clickhouse\.rights\b" CLAUDE.md
```

For each match, apply:

| Old | New |
|---|---|
| `Rights` (when referring to the type) | `Capabilities` |
| `UserSession` | `StoredSession` |
| `init_user_rights` | `provision_user` |
| `derive_rights` | `derive_capabilities` |
| `set_rights` | `set_capabilities` |
| `EMPTY_RIGHTS` | `EMPTY_CAPABILITIES` |
| `iris.auth.session` | `iris.auth.rights` |
| `iris.auth.sessions` | `iris.auth.store` |
| `iris.clickhouse.rights` | `iris.clickhouse.capabilities` |

Also update the Module map block (around line 96) to reflect the new layout:

```
old:
src/iris/
├── __init__.py        # main() + load_dotenv
├── app.py             # build_app(), Datastar routes, /, /api/greet, /api/clock
├── middleware.py      # SecurityHeadersMiddleware (CSP)
├── templates/         # Jinja2 — base.html + index.html
├── auth/              # auth subsystem — full surface in docs/auth.md
└── clickhouse/        # CH subsystem — full surface in docs/clickhouse.md

new:
src/iris/
├── __init__.py        # main() + load_dotenv
├── app.py             # build_app(), Datastar routes, /, /api/greet, /api/clock
├── middleware.py      # SecurityHeadersMiddleware (CSP)
├── templates/         # Jinja2 — base.html + index.html
├── auth/              # auth subsystem — User/StoredSession identity, Capabilities,
│                      # AuthSession views, SessionStore — full surface in docs/auth.md
└── clickhouse/        # CH subsystem — full surface in docs/clickhouse.md
```

- [ ] **Step 17.2: Update `docs/auth.md`**

This file has the most doc-side references. Apply the full substitution table from Step 17.1, plus update the type/module breakdown around lines 260-269:

```
old:
├── __init__.py        # public surface: AuthSession, Rights, EMPTY_RIGHTS,
                       #   Session*, User, install
...
├── session.py         # Rights frozen dataclass + serialization helpers + EMPTY_RIGHTS
├── identity.py        # User (frozen+slots), UserSession (mutable; internal),
                       #   AuthSession + database-bound subclasses
...
                       #   update_data / set_rights / delete / close

new:
├── __init__.py        # public surface: AuthSession, Capabilities, EMPTY_CAPABILITIES,
                       #   Session*, User, install
...
├── rights.py          # Capabilities frozen dataclass + serialization helpers + EMPTY_CAPABILITIES
├── identity.py        # User (frozen+slots), StoredSession (mutable; internal)
├── views.py           # AuthSession + database-bound subclasses
├── store.py           # SessionStore (SQLite); methods:
                       #   create / get_and_refresh / update_data / set_capabilities / delete / close
```

Around line 124-141 (the `Rights` → `Capabilities` section):

```
**The `Rights` dataclass:**
class Rights:
                ↓
**The `Capabilities` dataclass:**
class Capabilities:

`Rights` exposes three helpers — ...
                ↓
`Capabilities` exposes three helpers — ...

**How `derive_rights` works.** At login,
`iris.clickhouse.rights.derive_rights(client, username, groups)`:
                ↓
**How `derive_capabilities` works.** At login,
`iris.clickhouse.capabilities.derive_capabilities(client, username, groups)`:
```

Around line 203:

```
old: Both role types are created lazily by `init_user_rights` on each login.
new: Both role types are created lazily by `provision_user` on each login.
```

Sweep the rest of the file for any remaining `Rights` / `UserSession` / `init_user_rights` / `derive_rights` / `set_rights` references; update each.

- [ ] **Step 17.3: Update `docs/clickhouse.md`**

```
"- **User provisioning:** `init_user_rights`, `derive_rights`"
                ↓
"- **User provisioning:** `provision_user`, `derive_capabilities`"

"init_user_rights(client, username=\"alice\", ...)"  (in code example)
                ↓
"provision_user(client, username=\"alice\", ...)"

"All operations are idempotent: re-running is safe. `init_user_rights` reconciles ..."
                ↓
"All operations are idempotent: re-running is safe. `provision_user` reconciles ..."

"... once the target eventually authenticates, `init_user_rights` reuses the existing role and `derive_rights` picks up the tier membership."
                ↓
"... once the target eventually authenticates, `provision_user` reuses the existing role and `derive_capabilities` picks up the tier membership."

"1. `init_user_rights` — provisions the CH user/role/group memberships."
                ↓
"1. `provision_user` — provisions the CH user/role/group memberships."

"2. `derive_rights` — computes the `Rights` view from CH state ..."
                ↓
"2. `derive_capabilities` — computes the `Capabilities` view from CH state ..."

"3. `store.set_rights(session_id, rights)` — persists the `Rights` to the SQLite session row."
                ↓
"3. `store.set_capabilities(session_id, capabilities)` — persists the `Capabilities` to the SQLite session row."

"Cookie-based session refreshes do NOT re-provision; the cached `Rights` ..."
                ↓
"Cookie-based session refreshes do NOT re-provision; the cached `Capabilities` ..."

"... sessions land with `EMPTY_RIGHTS`, ..."
                ↓
"... sessions land with `EMPTY_CAPABILITIES`, ..."

(file structure block, around lines 148-149)
"├── rights.py        # derive_rights ..."
                ↓
"├── capabilities.py  # derive_capabilities ..."
"└── users.py         # init_user_rights, USER_ROLE_SUFFIX, GROUP_ROLE_SUFFIX"
                ↓
"└── users.py         # provision_user, USER_ROLE_SUFFIX, GROUP_ROLE_SUFFIX"
```

- [ ] **Step 17.4: Update `docs/operations.md`**

```
"... See `docs/clickhouse.md` for the full bootstrap behavior and the `derive_rights` detection logic."
                ↓
"... See `docs/clickhouse.md` for the full bootstrap behavior and the `derive_capabilities` detection logic."

"- **`derive_rights` query cost.** At login, `derive_rights` runs ..."
                ↓
"- **`derive_capabilities` query cost.** At login, `derive_capabilities` runs ..."

"... so `derive_rights` returns `is_admin=True`."
                ↓
"... so `derive_capabilities` returns `is_admin=True`."
```

Also append a new "Migration" section near the top (after the env-var table or wherever migration notes belong; if no such section exists, create one before the security-followups block):

```markdown
## Migration: 0.1.x → next

The `auth` package was reshaped: `Rights → Capabilities`, `UserSession → StoredSession`, plus module renames (`session.py → rights.py`, `sessions.py → store.py`, `clickhouse/rights.py → clickhouse/capabilities.py`). The SQLite session-store column `rights_json` was renamed to `capabilities_json`.

There is no in-code migration. Operators upgrading an existing instance must:

1. Stop iris.
2. Delete the SQLite file at `AUTH_DB_PATH` (default `./iris-auth.db`) plus its `.db-wal` and `.db-shm` sidecars.
3. Start iris.

All in-flight sessions are invalidated; users re-login. The 12 h sliding TTL means most sessions would have expired anyway.
```

- [ ] **Step 17.5: Verify all docs are clean**

```bash
grep -rn "iris\.auth\.session\b\|iris\.auth\.sessions\b\|iris\.clickhouse\.rights\b\|init_user_rights\|derive_rights\|set_rights\|EMPTY_RIGHTS\|UserSession" CLAUDE.md docs/auth.md docs/clickhouse.md docs/operations.md
```

Expected: zero matches.

For `Rights` (the bare class name), there may be legitimate prose mentions ("CH GRANTs are sometimes called rights"). Visually inspect the matches:

```bash
grep -rn "\bRights\b" CLAUDE.md docs/auth.md docs/clickhouse.md docs/operations.md
```

Update any line that means the `Capabilities` Python type; leave any line that uses "rights" as a generic English word.

- [ ] **Step 17.6: Do NOT commit.**

---

## Task 18: Final verification + atomic commit

**Files:** none modified — verification only, then a single commit covering everything from Tasks 2-17.

- [ ] **Step 18.1: Repo-wide grep — zero false positives expected**

```bash
grep -rn "iris\.auth\.session\b\|iris\.auth\.sessions\b\|iris\.clickhouse\.rights\b\|init_user_rights\|\bderive_rights\b\|\bset_rights\b\|\bEMPTY_RIGHTS\b\|\bUserSession\b" src/ tests/ CLAUDE.md docs/
```

Expected: zero matches.

For `\bRights\b` (the class name), do a focused review:

```bash
grep -rn "\bRights\b" src/ tests/ CLAUDE.md docs/
```

Inspect each match — Python source must have ZERO. Doc prose may have legitimate uses (e.g., "CH GRANT-derived rights"); inspect manually.

- [ ] **Step 18.2: Run ruff**

```bash
uv run --project /home/driou/dev/project/iris ruff check
```

Expected: zero warnings.

- [ ] **Step 18.3: Run basedpyright at error level**

```bash
uv run --project /home/driou/dev/project/iris basedpyright --level error
```

Expected: zero errors. If any error appears, it points at a forgotten reference. Fix it inline (no commit yet).

- [ ] **Step 18.4: Run basedpyright at warning level (the merge gate per CLAUDE.md)**

```bash
uv run --project /home/driou/dev/project/iris basedpyright --level warning
```

Expected: zero warnings. The CLAUDE.md gate is `--level warning`, not `--level error`.

- [ ] **Step 18.5: Run the unit suite**

```bash
uv run --project /home/driou/dev/project/iris pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
```

Expected: all green.

- [ ] **Step 18.6: Run the auth integration suite (Keycloak)**

```bash
uv run --project /home/driou/dev/project/iris pytest tests/auth/integration -q
```

Expected: all green. (Requires Docker.)

- [ ] **Step 18.7: Run the clickhouse integration suite (Keycloak + ClickHouse)**

```bash
uv run --project /home/driou/dev/project/iris pytest tests/clickhouse/integration -q
```

Expected: all green. (Requires Docker. The conftest spins up testcontainers; 1-2 minute warmup.)

- [ ] **Step 18.8: Test-inventory diff (no coverage regression)**

```bash
uv run --project /home/driou/dev/project/iris pytest --collect-only -q --ignore=tests/auth/integration --ignore=tests/clickhouse/integration > /tmp/pytest-inventory-after.txt
diff /tmp/pytest-inventory-before.txt /tmp/pytest-inventory-after.txt
```

Expected: only renames are visible (test_rights → test_capabilities; test_init_user_rights → test_provision_user). The TOTAL test count must be unchanged.

If a test disappeared, find it in the rename history and reconcile.

- [ ] **Step 18.9: Confirm `/api/whoami` JSON shape via existing tests (no manual smoke needed)**

The `tests/auth/test_session_dep.py` suite exercises `/api/whoami` end-to-end via `TestClient`. After the rename, those tests assert the response key is `"capabilities"` and the inner object's keys are unchanged. If pytest passed in Step 18.5 those assertions are green — no separate manual smoke run is required.

Sanity command:

```bash
grep -n '"capabilities"' tests/auth/test_session_dep.py
```

Expected: at least one match (the `/api/whoami` response-shape assertion).

- [ ] **Step 18.10: Review the staged diff**

```bash
git -C /home/driou/dev/project/iris status
git -C /home/driou/dev/project/iris diff --stat
git -C /home/driou/dev/project/iris diff --stat --staged
```

Expected: many files modified, with file renames detected by git (look for "rename:" lines). If git did not detect a rename (because content changed too much), that is acceptable — history can still be queried with `git log --follow`.

- [ ] **Step 18.11: Stage everything**

```bash
git -C /home/driou/dev/project/iris add -A
git -C /home/driou/dev/project/iris status
```

Verify the staging block lists every renamed/modified file explicitly. Watch for unintended files (e.g. stray `.db` or `.coverage`).

- [ ] **Step 18.12: Atomic commit**

```bash
git -C /home/driou/dev/project/iris commit -m "$(cat <<'EOF'
refactor(auth): reshape package — Rights→Capabilities, identity split, module renames

Implements the design at docs/superpowers/specs/2026-05-09-auth-module-reshape-design.md.

Module renames:
- src/iris/auth/session.py → src/iris/auth/rights.py (was 60 LOC; only Rights)
- src/iris/auth/sessions.py → src/iris/auth/store.py (SessionStore)
- src/iris/clickhouse/rights.py → src/iris/clickhouse/capabilities.py
- New: src/iris/auth/views.py (lifts AuthSession family out of identity.py)
- src/iris/auth/identity.py is trimmed to User + StoredSession only

Symbol renames:
- Rights → Capabilities (incl. EMPTY_RIGHTS → EMPTY_CAPABILITIES,
  rights_to_dict → capabilities_to_dict, rights_from_dict → capabilities_from_dict)
- UserSession → StoredSession
- init_user_rights → provision_user
- derive_rights → derive_capabilities
- SessionStore.set_rights → SessionStore.set_capabilities
- session.rights → session.capabilities (field on every AuthSession subclass)
- /api/whoami JSON key "rights" → "capabilities"
- SQLite column rights_json → capabilities_json (drop-and-recreate, no migration code)

Side benefits:
- The TYPE_CHECKING + pyright reportImportCycles=false workaround at the top
  of identity.py is gone — the cycle is broken by the file split, not silenced.

Cleanup riders:
- Drop unused logger from src/iris/auth/exceptions.py (U6).
- Drop spurious `async` from verify_csrf_form in src/iris/auth/csrf.py (U7).

Migration: operators must delete iris-auth.db (and .db-wal / .db-shm sidecars)
on deploy; in-flight sessions are invalidated. Documented in docs/operations.md.

Tests + ruff + basedpyright (warning level) green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 18.13: Verify the commit landed cleanly**

```bash
git -C /home/driou/dev/project/iris log -1 --stat
git -C /home/driou/dev/project/iris status
```

Expected: HEAD is the new commit; working tree clean.

---

## Out of scope (do NOT touch in this plan)

Per the spec — these are explicitly reserved for the security-hardening and SQL-hygiene specs:

- TokenBucket eviction (S1)
- Proxy-aware client IP (S2)
- CDN script SRI hash (S3)
- OAuth state cookie path (S4)
- CSRF on JSON requests (S5)
- `_safe_next` CRLF guard (S8)
- `_safe_next` info logging (U5)
- Database-name suffix validation
- `_FIXED_STRING_RE` deduplication (B5)
- `quote_string` vs `_marshal_array_element` escape unification (B6)
- `delete_database` orphan-grant sweep (U4)
- Any other naming polish (`tier_role_name`, `iris_global_admin` casing, etc.)
- Consolidation of `DatabaseAdminSession`'s 12 grant/revoke methods.

If anyone is tempted while doing the rename, leave it. A second pass will pick those up.
