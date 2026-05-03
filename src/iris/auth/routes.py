from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, Form, Request, Response
from fastapi.responses import RedirectResponse

from iris.auth.csrf import verify_csrf_form
from iris.auth.deps import CurrentUser
from iris.auth.exceptions import AuthError
from iris.auth.providers.base import Provider
from iris.auth.sessions import InMemorySessionStore

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
    # Reject backslashes (some browsers normalize \ -> / before same-origin check)
    if "\\" in next_url:
        return "/"
    # Reject anything that doesn't start with a single forward slash
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


def build_auth_router(
    *,
    provider: Provider,
    store: InMemorySessionStore,
    cookie_name: str,
    cookie_secure: bool,
    ttl_seconds: int,
) -> APIRouter:
    router = APIRouter()

    @router.get("/login")
    async def login_get(request: Request) -> Response:
        return await provider.begin(request)

    @router.post("/login")
    async def login_post(
        request: Request,
        username: str = Form(default=""),
        password: str = Form(default=""),
        next: str = Form(default="/"),
        _: None = Depends(verify_csrf_form),
    ) -> Response:
        if not hasattr(provider, "authenticate"):
            return Response(status_code=405)  # OAuth doesn't go through POST /login
        safe_next = _safe_next(next)
        try:
            user = await provider.authenticate(username, password)
        except AuthError as err:
            return RedirectResponse(
                f"/login?{urlencode({'error': err.token, 'next': safe_next})}",
                status_code=302,
            )
        session = await store.create(user)
        logger.info(
            "auth: login user=%s subject=%s method=form groups=%s",
            user.display_name,
            user.subject,
            list(user.groups),
        )
        response = RedirectResponse(safe_next, status_code=302)
        _set_session_cookie(
            response,
            name=cookie_name,
            sid=session.id,
            ttl=ttl_seconds,
            secure=cookie_secure,
        )
        return response

    @router.post("/logout")
    async def logout(
        request: Request,
        user: CurrentUser,
        _: None = Depends(verify_csrf_form),
    ) -> Response:
        sid = request.cookies.get(cookie_name) or ""
        if sid:
            await store.delete(sid)
        logger.info("auth: logout user=%s subject=%s", user.display_name, user.subject)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(cookie_name)
        return response

    @router.get("/api/whoami")
    async def whoami(user: CurrentUser) -> dict:
        return {
            "subject": user.subject,
            "display_name": user.display_name,
            "groups": list(user.groups),
        }

    return router


def install(app: FastAPI) -> None:
    """Wire the auth package into a FastAPI app: settings, store, exception handlers, router."""
    from iris.auth.config import AuthSettings
    from iris.auth.deps import set_session_store, set_settings
    from iris.auth.exceptions import install_exception_handlers
    from iris.auth.providers import build_provider

    settings = AuthSettings.from_env()
    store = InMemorySessionStore(ttl_seconds=settings.ttl_seconds)
    provider = build_provider(settings)

    set_session_store(app, store)
    set_settings(
        app, cookie_name=settings.cookie_name, cookie_secure=settings.cookie_secure
    )
    install_exception_handlers(app, cookie_name=settings.cookie_name)

    router = build_auth_router(
        provider=provider,
        store=store,
        cookie_name=settings.cookie_name,
        cookie_secure=settings.cookie_secure,
        ttl_seconds=settings.ttl_seconds,
    )
    app.include_router(router)
