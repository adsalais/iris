from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse

logger = logging.getLogger("iris.auth")


class AuthRequired(Exception):
    """Raised when no valid session is present."""


class AuthForbidden(Exception):
    """Raised when the authenticated user lacks a required right."""

    def __init__(self, *, needed: tuple[str, ...], have: tuple[str, ...]) -> None:
        super().__init__(f"need one of {needed}, have {have}")
        self.needed = needed
        self.have = have


class AuthError(Exception):
    """Provider-side authentication failure with a stable error token."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def install_exception_handlers(app: FastAPI, *, cookie_name: str) -> None:
    @app.exception_handler(AuthRequired)
    async def _on_auth_required(request: Request, _exc: AuthRequired) -> Response:
        if _wants_html(request):
            response = RedirectResponse(
                f"/login?next={request.url.path}", status_code=302
            )
            response.delete_cookie(cookie_name)
            return response
        return Response(status_code=401)

    @app.exception_handler(AuthForbidden)
    async def _on_auth_forbidden(request: Request, exc: AuthForbidden) -> Response:
        if _wants_html(request):
            templates = request.app.state.templates
            return templates.TemplateResponse(
                request,
                "auth/forbidden.html",
                {"needed": list(exc.needed), "have": list(exc.have)},
                status_code=403,
            )
        return Response(status_code=403)
