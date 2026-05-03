from __future__ import annotations

import hmac

from fastapi import Request, Response
from fastapi.responses import HTMLResponse

from iris.auth.config import MockSettings
from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD, issue_csrf_token
from iris.auth.exceptions import AuthError
from iris.auth.identity import User


_FORM_HTML = """\
<!doctype html><html><body>
<h1>Sign in</h1>
{error}
<form method="post" action="/login">
  <input type="hidden" name="{csrf_field}" value="{csrf}">
  <input type="hidden" name="next" value="{next_url}">
  <label>Username <input name="username" required></label>
  <label>Password <input type="password" name="password" required></label>
  <button type="submit">Sign in</button>
</form>
</body></html>
"""


class MockProvider:
    def __init__(self, settings: MockSettings) -> None:
        self._settings = settings

    async def begin(self, request: Request) -> Response:
        response = HTMLResponse("")  # body filled below after issuing token
        token = issue_csrf_token(request, response)
        next_url = request.query_params.get("next", "/")
        error = ""
        if err := request.query_params.get("error"):
            error = f'<p style="color:red">Error: {err}</p>'
        response.body = _FORM_HTML.format(
            csrf=token,
            csrf_field=CSRF_FORM_FIELD,
            next_url=next_url,
            error=error,
        ).encode()
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
