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
from iris.auth.identity import (
    AdminSession,
    AuthSession,
    DatabaseAdminSession,
    DatabaseCreatorSession,
    DatabaseSession,
    UserSession,
)
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


async def _resolve_stored(request: Request) -> UserSession | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name)
    if not sid:
        return None
    store = _get_store(request)
    return await store.get_and_refresh(sid)


_StoredSession = Annotated[UserSession | None, Depends(_resolve_stored)]


def _to_auth_session(stored: UserSession, request: Request) -> AuthSession:
    client, http_client, settings = _ch_refs(request)
    return AuthSession(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        rights=stored.rights,
        client=client,
        http_client=http_client,
        settings=settings,
    )


async def _optional_session(
    request: Request, stored: _StoredSession
) -> AuthSession | None:
    if stored is None:
        return None
    return _to_auth_session(stored, request)


async def _require_session(
    request: Request, stored: _StoredSession
) -> AuthSession:
    if stored is None:
        raise AuthRequired()
    return _to_auth_session(stored, request)


_RequiredAuth = Annotated[AuthSession, Depends(_require_session)]


async def _require_admin(session: _RequiredAuth) -> AdminSession:
    if not session.rights.is_admin:
        raise AuthForbidden(needed=("admin",), have=())
    return AdminSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
    )


async def _require_database_creator(
    session: _RequiredAuth,
) -> DatabaseCreatorSession:
    r = session.rights
    if not (r.is_admin or r.can_create_database):
        raise AuthForbidden(needed=("admin", "database_creator"), have=())
    return DatabaseCreatorSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
    )


async def _require_database_admin(
    database: str, session: _RequiredAuth
) -> DatabaseAdminSession:
    if not session.rights.has_admin(database):
        raise AuthForbidden(needed=(f"database_admin[{database}]",), have=())
    return DatabaseAdminSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        database=database,
    )


async def _require_write(
    database: str, session: _RequiredAuth
) -> DatabaseSession:
    if not session.rights.has_write(database):
        raise AuthForbidden(needed=(f"database_writer[{database}]",), have=())
    return DatabaseSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
        database=database,
    )


async def _require_read(
    database: str, session: _RequiredAuth
) -> DatabaseSession:
    if not session.rights.has_read(database):
        raise AuthForbidden(needed=(f"database_reader[{database}]",), have=())
    return DatabaseSession(
        id=session.id,
        user=session.user,
        created_at=session.created_at,
        expires_at=session.expires_at,
        data=session.data,
        rights=session.rights,
        client=session.client,
        http_client=session.http_client,
        settings=session.settings,
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
