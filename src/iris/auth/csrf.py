from __future__ import annotations

import hmac
import re
import secrets

from fastapi import Form, HTTPException, Request, Response

CSRF_COOKIE_NAME = "iris_csrf"
CSRF_FORM_FIELD = "_csrf_token"

# Well-formed CSRF tokens are urlsafe-base64 (the alphabet
# secrets.token_urlsafe emits) of at least 32 characters — enough entropy
# for a CSRF defense and matching what mint_csrf_token issues.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def mint_csrf_token(request: Request) -> str:
    """Return the CSRF token: reuse the cookie value if well-formed, else mint fresh.

    A well-formed token matches ``[A-Za-z0-9_-]{32,128}`` (urlsafe-base64,
    minimum entropy of ``secrets.token_urlsafe(24)``). Anything else —
    including attacker-supplied cookie values — is replaced with a fresh
    ``secrets.token_urlsafe(32)`` so a chosen value can't persist into
    later forms.
    """
    existing = request.cookies.get(CSRF_COOKIE_NAME, "")
    if existing and _TOKEN_RE.fullmatch(existing):
        return existing
    return secrets.token_urlsafe(32)


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
