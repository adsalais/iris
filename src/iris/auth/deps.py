from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Request

from iris.auth.exceptions import AuthForbidden, AuthRequired
from iris.auth.identity import User
from iris.auth.sessions import InMemorySessionStore


def set_session_store(app: FastAPI, store: InMemorySessionStore) -> None:
    app.state.auth_session_store = store


def set_settings(app: FastAPI, *, cookie_name: str, cookie_secure: bool = True) -> None:
    app.state.auth_cookie_name = cookie_name
    app.state.auth_cookie_secure = cookie_secure


def _get_store(request: Request) -> InMemorySessionStore:
    return request.app.state.auth_session_store


def _get_cookie_name(request: Request) -> str:
    return request.app.state.auth_cookie_name


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def _resolve(request: Request) -> User | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name) or _bearer(request.headers.get("authorization"))
    if not sid:
        return None
    store = _get_store(request)
    session = await store.get_and_refresh(sid)
    return session.user if session else None


async def _current_user(request: Request) -> User:
    user = await _resolve(request)
    if user is None:
        raise AuthRequired()
    return user


async def _optional_current_user(request: Request) -> User | None:
    return await _resolve(request)


CurrentUser = Annotated[User, Depends(_current_user)]
OptionalCurrentUser = Annotated[User | None, Depends(_optional_current_user)]


def require_group(*groups: str):
    async def _check(user: CurrentUser) -> User:
        if not set(groups) & set(user.groups):
            raise AuthForbidden(needed=groups, have=user.groups)
        return user

    return _check
