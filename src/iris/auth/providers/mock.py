from __future__ import annotations

import hmac

from fastapi import Request, Response

from iris.auth.config import MockSettings
from iris.auth.exceptions import AuthError
from iris.auth.providers._form import render_login_form
from iris.auth.identity import User


class MockProvider:
    def __init__(self, settings: MockSettings) -> None:
        self._settings = settings

    async def begin(self, request: Request) -> Response:
        return render_login_form(
            request,
            {
                "invalid_credentials": "Invalid username or password.",
                "csrf_mismatch": "Session expired, please reload and try again.",
            },
        )

    async def authenticate(self, username: str, password: str) -> User:
        if not hmac.compare_digest(username, self._settings.username) or not hmac.compare_digest(
            password, self._settings.password
        ):
            raise AuthError("invalid_credentials")
        return User(
            subject=f"mock:{self._settings.username}",
            username=self._settings.username,
            display_name=self._settings.display_name,
            groups=self._settings.groups,
        )
