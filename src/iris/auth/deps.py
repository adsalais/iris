"""FastAPI dependency aliases for the CH-only authorization model.

Routes consume these as type annotations:

    @app.get("/me")
    async def me(session: Session) -> dict: ...

    @app.get("/db/{database}/read")
    async def read_db(database: str, session: SessionRead) -> ...: ...

The aliases bake in ``Depends(...)`` so the route author doesn't write
``= Depends(...)`` after the type. Each alias delegates rights checks to
``session.rights`` (the frozen ``Rights`` view computed at login by
``iris.clickhouse.rights.derive_rights``).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Request

from iris.auth.exceptions import AuthForbidden, AuthRequired
from iris.auth.identity import AuthSession, UserSession
from iris.auth.sessions import SessionStore


def set_session_store(app: FastAPI, store: SessionStore) -> None:
    app.state.auth_session_store = store


def set_settings(app: FastAPI, *, cookie_name: str, cookie_secure: bool = True) -> None:
    app.state.auth_cookie_name = cookie_name
    app.state.auth_cookie_secure = cookie_secure


def _get_store(request: Request) -> SessionStore:
    return request.app.state.auth_session_store


def _get_cookie_name(request: Request) -> str:
    return request.app.state.auth_cookie_name


async def _resolve_stored(request: Request) -> UserSession | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name)
    if not sid:
        return None
    store = _get_store(request)
    return await store.get_and_refresh(sid)


_StoredSession = Annotated[UserSession | None, Depends(_resolve_stored)]


def _to_view(stored: UserSession) -> AuthSession:
    return AuthSession(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        rights=stored.rights,
    )


async def _optional_session(stored: _StoredSession) -> AuthSession | None:
    if stored is None:
        return None
    return _to_view(stored)


async def _require_session(stored: _StoredSession) -> AuthSession:
    if stored is None:
        raise AuthRequired()
    return _to_view(stored)


_RequiredAuth = Annotated[AuthSession, Depends(_require_session)]


async def _require_admin(session: _RequiredAuth) -> AuthSession:
    if not session.rights.is_admin:
        raise AuthForbidden(needed=("admin",), have=())
    return session


async def _require_database_creator(session: _RequiredAuth) -> AuthSession:
    r = session.rights
    if not (r.is_admin or r.can_create_database):
        raise AuthForbidden(needed=("admin", "database_creator"), have=())
    return session


async def _require_database_admin(
    database: str, session: _RequiredAuth
) -> AuthSession:
    if not session.rights.has_admin(database):
        raise AuthForbidden(needed=(f"database_admin[{database}]",), have=())
    return session


async def _require_write(database: str, session: _RequiredAuth) -> AuthSession:
    if not session.rights.has_write(database):
        raise AuthForbidden(needed=(f"database_writer[{database}]",), have=())
    return session


async def _require_read(database: str, session: _RequiredAuth) -> AuthSession:
    if not session.rights.has_read(database):
        raise AuthForbidden(needed=(f"database_reader[{database}]",), have=())
    return session


# Public Annotated aliases — what routes consume.
Session = Annotated[AuthSession, Depends(_require_session)]
SessionOptional = Annotated[AuthSession | None, Depends(_optional_session)]
SessionAdmin = Annotated[AuthSession, Depends(_require_admin)]
SessionDatabaseCreator = Annotated[AuthSession, Depends(_require_database_creator)]
SessionDatabaseAdmin = Annotated[AuthSession, Depends(_require_database_admin)]
SessionWrite = Annotated[AuthSession, Depends(_require_write)]
SessionRead = Annotated[AuthSession, Depends(_require_read)]
