from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from iris.auth.identity import User


@dataclass(frozen=True, slots=True)
class Session:
    """Request-scoped view of a logged-in session (legacy shape).

    Replaced by :class:`AuthSession` once the CH-only authorization migration
    completes. Until then both shapes coexist: ``Session`` is what current code
    consumes; ``AuthSession`` is what the new alias deps will return.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    roles: frozenset[str]


@dataclass(frozen=True, slots=True)
class Rights:
    """Frozen view of a session's effective ClickHouse-derived authorization.

    Computed once at login by ``iris.clickhouse.rights.derive_rights`` and
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


@dataclass(frozen=True, slots=True)
class AuthSession:
    """Request-scoped view of a logged-in session, with derived ``Rights``.

    Built once per request by the auth dep. Routes receive an ``AuthSession``
    via one of the ``Annotated`` alias deps in ``iris.auth.deps``: ``Session``
    (require auth), ``SessionOptional`` (admit None), ``SessionRead`` /
    ``SessionWrite`` / ``SessionDatabaseAdmin`` (database-scoped tier checks
    via ``rights``), ``SessionDatabaseCreator`` / ``SessionAdmin``.

    Frozen except for ``data``: the dict is a per-request snapshot deserialized
    from the SQLite session store. Mutations to the dict do NOT auto-persist —
    call ``await request.app.state.auth_session_store.update_data(session.id,
    session.data)`` to write changes back.
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    rights: Rights


def rights_to_dict(r: Rights) -> dict[str, Any]:
    return {
        "is_admin": r.is_admin,
        "can_create_database": r.can_create_database,
        "db_admin": sorted(r.db_admin),
        "db_writer": sorted(r.db_writer),
        "db_reader": sorted(r.db_reader),
    }


def rights_from_dict(d: dict[str, Any]) -> Rights:
    return Rights(
        is_admin=bool(d.get("is_admin", False)),
        can_create_database=bool(d.get("can_create_database", False)),
        db_admin=frozenset(d.get("db_admin", [])),
        db_writer=frozenset(d.get("db_writer", [])),
        db_reader=frozenset(d.get("db_reader", [])),
    )


EMPTY_RIGHTS = Rights(
    is_admin=False,
    can_create_database=False,
    db_admin=frozenset(),
    db_writer=frozenset(),
    db_reader=frozenset(),
)
