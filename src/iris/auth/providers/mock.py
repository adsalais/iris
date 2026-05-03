from __future__ import annotations

import hmac

from fastapi import Request, Response

from iris.auth.config import MockSettings
from iris.auth.csrf import (
    CSRF_FORM_FIELD,
    attach_csrf_cookie,
    mint_csrf_token,
)
from iris.auth.exceptions import AuthError
from iris.auth.identity import User


class MockProvider:
    def __init__(self, settings: MockSettings) -> None:
        self._settings = settings

    async def begin(self, request: Request) -> Response:
        templates = request.app.state.templates
        next_url = request.query_params.get("next", "/")
        error = request.query_params.get("error")
        error_message = (
            {
                "invalid_credentials": "Invalid username or password.",
                "csrf_mismatch": "Session expired, please reload and try again.",
            }.get(error or "", "An error occurred.")
            if error
            else ""
        )
        token = mint_csrf_token(request)
        response = templates.TemplateResponse(
            request,
            "auth/ldap_form.html",
            {
                "csrf_field": CSRF_FORM_FIELD,
                "csrf_token": token,
                "next_url": next_url,
                "error": bool(error),
                "error_message": error_message,
            },
        )
        attach_csrf_cookie(request, response, token)
        return response

    async def complete(self, request: Request) -> User:
        # the route layer (Task 8) extracts username/password from the POST body
        # and calls authenticate(); complete() exists to satisfy the Protocol but
        # is intentionally not called for form-based providers.
        raise NotImplementedError("MockProvider uses authenticate()")

    async def authenticate(self, username: str, password: str) -> User:
        if not hmac.compare_digest(username, self._settings.username) or not hmac.compare_digest(
            password, self._settings.password
        ):
            raise AuthError("invalid_credentials")
        return User(
            subject=f"mock:{self._settings.username}",
            display_name=self._settings.display_name,
            groups=self._settings.groups,
        )
