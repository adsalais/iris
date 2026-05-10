from __future__ import annotations

from typing import Protocol

from fastapi import Request, Response


class Provider(Protocol):
    async def begin(self, request: Request) -> Response: ...

    async def end_session_url(self, post_logout_redirect: str | None = None) -> str | None:
        """IdP-side logout URL, or None if the provider has no IdP session.

        For OIDC providers this is the discovered `end_session_endpoint`;
        without it, deleting iris's session cookie is not enough — the IdP
        still has an SSO cookie and silently re-authenticates the user on
        the next /login round-trip.

        ``post_logout_redirect``: if non-None, the IdP is asked to redirect
        the browser there after sign-out. The URL must be registered as a
        valid post-logout redirect URI in the IdP client config.
        """
        ...
