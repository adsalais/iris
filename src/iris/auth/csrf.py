from __future__ import annotations

import hmac
import secrets

from fastapi import Form, HTTPException, Request, Response

CSRF_COOKIE_NAME = "iris_csrf"
CSRF_FORM_FIELD = "_csrf_token"


def mint_csrf_token(request: Request) -> str:
    """Return the CSRF token: reuse the cookie value if present, else generate a new one."""
    return request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(32)


def attach_csrf_cookie(request: Request, response: Response, token: str) -> None:
    """Set the CSRF cookie carrying `token` on the response."""
    secure = getattr(request.app.state, "auth_cookie_secure", True)
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=60 * 60,
        httponly=False,  # readable by JS-rendered forms; not security-critical for this token
        secure=secure,
        samesite="lax",
        path="/",
    )


def issue_csrf_token(request: Request, response: Response) -> str:
    """Mint a token and attach the cookie in one step.

    Used by callers that don't need to embed the token in the rendered body
    (e.g., GET endpoints that bootstrap the cookie without a form).

    WARNING: Only safe to use as `Depends(issue_csrf_token)` on routes that
    return a non-Response body (dict, Pydantic model, str, etc.). FastAPI
    only merges cookies from the dep-injected `Response` parameter when it
    builds the final Response itself. For routes that return a Response
    directly (HTMLResponse, RedirectResponse, TemplateResponse, ...), call
    `mint_csrf_token(request)` + `attach_csrf_cookie(request, response, token)`
    on the actual response — see `iris.app.index` for the pattern.
    """
    token = mint_csrf_token(request)
    attach_csrf_cookie(request, response, token)
    return token


async def verify_csrf_form(
    request: Request,
    csrf_token: str = Form(default="", alias=CSRF_FORM_FIELD),
) -> None:
    cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not cookie or not csrf_token or not hmac.compare_digest(cookie, csrf_token):
        raise HTTPException(status_code=400, detail="csrf_mismatch")


def delete_csrf_cookie(response: Response) -> None:
    """Clear the CSRF cookie. Used after auth boundaries (login) to rotate."""
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
