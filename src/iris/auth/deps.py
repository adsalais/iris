from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Request

from iris.auth.authz.core import CurrentMapping, resolve_roles
from iris.auth.exceptions import AuthRequired
from iris.auth.identity import UserSession
from iris.auth.session import Session as _SessionT
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


def _bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def _resolve_stored(request: Request) -> UserSession | None:
    cookie_name = _get_cookie_name(request)
    sid = request.cookies.get(cookie_name) or _bearer(
        request.headers.get("authorization")
    )
    if not sid:
        return None
    store = _get_store(request)
    return await store.get_and_refresh(sid)


_StoredSession = Annotated[UserSession | None, Depends(_resolve_stored)]


async def _build_optional(
    stored: _StoredSession,
    mapping: CurrentMapping,
) -> _SessionT | None:
    if stored is None:
        return None
    return _SessionT(
        id=stored.id,
        user=stored.user,
        created_at=stored.created_at,
        expires_at=stored.expires_at,
        data=stored.data,
        roles=resolve_roles(stored.user, mapping),
    )


_BuiltOptional = Annotated[_SessionT | None, Depends(_build_optional)]


async def _build_required(view: _BuiltOptional) -> _SessionT:
    if view is None:
        raise AuthRequired()
    return view


Session = Annotated[_SessionT, Depends(_build_required)]
OptionalSession = Annotated[_SessionT | None, Depends(_build_optional)]
