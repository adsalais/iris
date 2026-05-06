from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Request

from iris.auth.authz.core import CurrentMapping, resolve_roles
from iris.auth.exceptions import AuthRequired
from iris.auth.identity import UserSession
from iris.auth.session import SessionView
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


async def optional_session(
    stored: _StoredSession,
    mapping: CurrentMapping,
) -> SessionView | None:
    """FastAPI dep: returns a ``SessionView`` if the request has a valid
    session cookie, ``None`` otherwise. Use as
    ``session: SessionView | None = Depends(optional_session)`` on routes
    that work with or without an authenticated user.
    """
    if stored is None:
        return None
    return SessionView(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        roles=resolve_roles(stored.user, mapping),
    )


_OptionalSessionDep = Annotated[SessionView | None, Depends(optional_session)]


async def require_session(view: _OptionalSessionDep) -> SessionView:
    """FastAPI dep: returns the request's ``SessionView`` or raises
    ``AuthRequired`` (401) if no session cookie is present. Use as
    ``session: SessionView = Depends(require_session)`` on routes that
    need an authenticated user without a specific role check.
    """
    if view is None:
        raise AuthRequired()
    return view
