from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from iris.auth.config import OIDCSettings
from iris.auth.exceptions import AuthError
from iris.auth.identity import User

logger = logging.getLogger("iris.auth.oauth")

OAUTH_STATE_COOKIE = "oauth_state"
STATE_COOKIE_TTL = 600  # 10 minutes


class OAuthProvider:
    def __init__(
        self,
        settings: OIDCSettings,
        *,
        _http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._client = httpx.Client(transport=_http_transport, timeout=10.0)
        self._async_client = httpx.AsyncClient(transport=_http_transport, timeout=10.0)
        self._signer = URLSafeTimedSerializer(settings.client_secret, salt="iris-oauth-state")
        # Discovery at construction — fail loudly if unreachable
        discovery_url = settings.issuer_url.rstrip("/") + "/.well-known/openid-configuration"
        try:
            doc = self._client.get(discovery_url).raise_for_status().json()
        except Exception as exc:
            raise RuntimeError(f"OIDC discovery failed for {discovery_url}: {exc}") from exc
        self.authorize_endpoint: str = doc["authorization_endpoint"]
        self.token_endpoint: str = doc["token_endpoint"]
        self.userinfo_endpoint: str = doc["userinfo_endpoint"]

    async def begin(self, request: Request) -> Response:
        redirect_uri = str(request.url_for("login_callback"))
        url, state, verifier = self.build_authorize_url(redirect_uri=redirect_uri)
        next_url = request.query_params.get("next", "/")
        signed = self._signer.dumps({"state": state, "verifier": verifier, "next": next_url})
        secure = getattr(request.app.state, "auth_cookie_secure", True)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(
            OAUTH_STATE_COOKIE,
            signed,
            max_age=STATE_COOKIE_TTL,
            httponly=True,
            secure=secure,
            samesite="lax",
        )
        return response

    async def complete(self, request: Request) -> tuple[User, str]:
        """Returns (user, next_url) on success."""
        signed = request.cookies.get(OAUTH_STATE_COOKIE)
        if not signed:
            raise AuthError("oauth_state")
        try:
            payload = self._signer.loads(signed, max_age=STATE_COOKIE_TTL)
        except BadSignature:
            raise AuthError("oauth_state")
        if request.query_params.get("state") != payload["state"]:
            raise AuthError("oauth_state")
        code = request.query_params.get("code", "")
        if not code:
            raise AuthError("oauth_exchange")
        user = await self.exchange_code(
            code=code,
            code_verifier=payload["verifier"],
            redirect_uri=str(request.url_for("login_callback")),
        )
        return user, payload.get("next", "/")

    def build_authorize_url(self, *, redirect_uri: str) -> tuple[str, str, str]:
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        params = {
            "response_type": "code",
            "client_id": self._settings.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self._settings.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.authorize_endpoint}?{urlencode(params)}", state, verifier

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> User:
        try:
            r = await self._async_client.post(
                self.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._settings.client_id,
                    "client_secret": self._settings.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
            )
            r.raise_for_status()
            access_token = r.json()["access_token"]
            ui = await self._async_client.get(
                self.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            ui.raise_for_status()
            claims = ui.json()
        except Exception as exc:
            logger.exception("auth: OAuth code exchange failed")
            raise AuthError("oauth_exchange") from exc
        groups = tuple(claims.get("groups") or ())
        if not groups:
            logger.warning(
                "auth: OAuth userinfo had no `groups` claim — check IdP client mapper"
            )
        return User(
            subject=str(claims["sub"]),
            display_name=str(claims.get("name") or claims.get("preferred_username") or claims["sub"]),
            groups=groups,
        )

    # OAuth provider has no .authenticate(username, password); the route layer
    # calls .begin() and .complete() instead.
