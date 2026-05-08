from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, Form, Request, Response
from fastapi.responses import RedirectResponse

from iris.auth.csrf import delete_csrf_cookie, verify_csrf_form
from iris.auth.deps import Session
from iris.auth.exceptions import AuthError
from iris.auth.identity import User
from iris.auth.providers.base import Provider
from iris.auth.providers.ldap import LDAPProvider
from iris.auth.providers.mock import MockProvider
from iris.auth.providers.oauth import OAUTH_STATE_COOKIE, OAuthProvider
from iris.auth.rate_limit import TokenBucket
from iris.auth.sessions import SessionStore

logger = logging.getLogger("iris.auth")


def _set_session_cookie(
    response: Response, *, name: str, sid: str, ttl: int, secure: bool
) -> None:
    response.set_cookie(
        name,
        sid,
        max_age=ttl,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _safe_next(next_url: str) -> str:
    """Return next_url only if it's a same-origin relative path; else /."""
    if not next_url:
        return "/"
    if "\\" in next_url:
        return "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def build_auth_router(
    *,
    app: FastAPI,
    provider: Provider,
    store: SessionStore,
    cookie_name: str,
    cookie_secure: bool,
    ttl_seconds: int,
) -> APIRouter:
    router = APIRouter()
    login_bucket = TokenBucket(capacity=10, refill_per_second=0.2)

    async def _finalize_login_redirect(
        *, user: User, target: str, method: str
    ) -> RedirectResponse:
        session = await store.create(user)
        for hook in app.state.post_login_hooks:
            await hook(user, session.id)
        logger.info(
            "auth: login user=%s subject=%s method=%s groups=%s",
            user.display_name,
            user.subject,
            method,
            list(user.groups),
        )
        response = RedirectResponse(target, status_code=302)
        _set_session_cookie(
            response,
            name=cookie_name,
            sid=session.id,
            ttl=ttl_seconds,
            secure=cookie_secure,
        )
        delete_csrf_cookie(response)
        return response

    @router.get("/login")
    async def login_get(request: Request) -> Response:
        return await provider.begin(request)

    @router.post("/login")
    async def login_post(
        request: Request,
        username: str = Form(default="", max_length=64),
        password: str = Form(default="", max_length=4096),
        next: str = Form(default="/", max_length=2048),
        _: None = Depends(verify_csrf_form),
    ) -> Response:
        client_host = request.client.host if request.client else "unknown"
        wait = login_bucket.take(f"login:{client_host}")
        if wait is not None:
            logger.info(
                "auth: login_rate_limited remote_addr=%s wait_seconds=%.1f",
                client_host,
                wait,
            )
            return Response(
                status_code=429,
                headers={"Retry-After": str(int(wait) + 1)},
            )
        if not isinstance(provider, (LDAPProvider, MockProvider)):
            return Response(status_code=405)
        safe_next = _safe_next(next)
        try:
            user = await provider.authenticate(username, password)
        except AuthError as err:
            logger.info(
                "auth: login_failed username=%s reason=%s remote_addr=%s",
                username,
                err.token,
                client_host,
            )
            return RedirectResponse(
                f"/login?{urlencode({'error': err.token, 'next': safe_next})}",
                status_code=302,
            )
        return await _finalize_login_redirect(user=user, target=safe_next, method="form")

    @router.get("/login/callback", name="login_callback")
    async def login_callback(request: Request) -> Response:
        if not isinstance(provider, OAuthProvider):
            return Response(status_code=404)
        try:
            user, next_url = await provider.complete(request)
        except AuthError as err:
            return RedirectResponse(f"/login?error={err.token}", status_code=302)
        safe_next = _safe_next(next_url)
        response = await _finalize_login_redirect(user=user, target=safe_next, method="oauth")
        response.delete_cookie(OAUTH_STATE_COOKIE)
        return response

    @router.post("/logout")
    async def logout(
        request: Request,
        session: Session,
        _: None = Depends(verify_csrf_form),
    ) -> Response:
        sid = request.cookies.get(cookie_name) or ""
        if sid:
            await store.delete(sid)
        logger.info(
            "auth: logout user=%s subject=%s",
            session.user.display_name,
            session.user.subject,
        )
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(cookie_name)
        return response

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

    return router


def install(app: FastAPI) -> None:
    """Wire the auth package into a FastAPI app: settings, store, exception handlers, router."""
    from iris.auth.config import AuthSettings
    from iris.auth.deps import set_session_store, set_settings
    from iris.auth.exceptions import install_exception_handlers
    from iris.auth.providers import build_provider

    settings = AuthSettings.from_env()
    app.state.auth_db_path = settings.auth_db_path
    app.state.auth_bootstrap_user = settings.bootstrap_user

    store = SessionStore(
        path=settings.auth_db_path,
        ttl_seconds=settings.ttl_seconds,
        absolute_ttl_seconds=settings.absolute_ttl_seconds,
        max_per_user=settings.max_per_user,
    )
    app.state.auth_close_session_store = store.close
    provider = build_provider(settings)

    from iris.templates import TEMPLATES
    app.state.templates = TEMPLATES

    set_session_store(app, store)
    set_settings(
        app, cookie_name=settings.cookie_name, cookie_secure=settings.cookie_secure
    )
    install_exception_handlers(app, cookie_name=settings.cookie_name)

    app.state.post_login_hooks = []

    router = build_auth_router(
        app=app,
        provider=provider,
        store=store,
        cookie_name=settings.cookie_name,
        cookie_secure=settings.cookie_secure,
        ttl_seconds=settings.ttl_seconds,
    )
    app.include_router(router)

    if isinstance(provider, OAuthProvider):
        app.state.auth_close_provider = provider.close
