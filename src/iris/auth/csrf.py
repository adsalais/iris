from __future__ import annotations

import hmac
import secrets

from fastapi import Form, HTTPException, Request, Response

CSRF_COOKIE_NAME = "iris_csrf"
CSRF_FORM_FIELD = "_csrf_token"


def issue_csrf_token(request: Request, response: Response) -> str:
    token = request.cookies.get(CSRF_COOKIE_NAME)
    if not token:
        token = secrets.token_urlsafe(32)
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
    return token


async def verify_csrf_form(
    request: Request,
    csrf_token: str = Form(default="", alias=CSRF_FORM_FIELD),
) -> None:
    cookie = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not cookie or not csrf_token or not hmac.compare_digest(cookie, csrf_token):
        raise HTTPException(status_code=400, detail="csrf_mismatch")
