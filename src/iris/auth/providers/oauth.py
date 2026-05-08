from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import ssl
from typing import Any, cast
from urllib.parse import urlencode


import httpx
import jwt
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
    """OIDC authorization-code-with-PKCE provider.

    Construction does no I/O. Discovery (``/.well-known/openid-configuration``)
    and JWKS fetch happen on first use, guarded by ``self._discovery_lock``
    so concurrent first requests trigger exactly one network round-trip.
    All discovery + token + userinfo I/O goes through a single
    ``httpx.AsyncClient`` so the event loop never blocks.

    State cookie signing: ``URLSafeTimedSerializer`` is keyed by a SHA-256
    derivation of ``client_secret`` (prefixed with
    ``iris-oauth-state-signing-v1:``) so a leak of the signing key is not
    a leak of the OAuth client secret. The ``v1`` tag lets us rotate the
    derivation in a future release without invalidating in-flight state
    cookies mid-deploy.

    Limitation: JWKS is cached on first discovery; IdP key rotation
    requires an app restart. Acceptable for v1; revisit if rotation matters.
    """

    def __init__(
        self,
        settings: OIDCSettings,
        *,
        _http_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        # When _http_transport is set (offline tests), the transport replaces
        # httpx's network stack entirely and `verify` is irrelevant. When it's
        # None, honor settings.ca_cert_path so a private CA can sign the
        # IdP cert.
        verify_arg: bool | ssl.SSLContext = True
        if settings.ca_cert_path:
            verify_arg = ssl.create_default_context(cafile=settings.ca_cert_path)
        if _http_transport is not None:
            # httpx.MockTransport implements both sync and async dispatch but
            # only inherits from BaseTransport. Pyright sees BaseTransport and
            # AsyncBaseTransport as unrelated; the double cast through object
            # bypasses that check while preserving the runtime behavior.
            self._async_client = httpx.AsyncClient(
                transport=cast("httpx.AsyncBaseTransport", cast(object, _http_transport)),
                timeout=10.0,
            )
        else:
            self._async_client = httpx.AsyncClient(verify=verify_arg, timeout=10.0)
        # Derive the state-signing key from client_secret so a leak of one is
        # not a leak of the other. The "v1" tag in the prefix lets us rotate
        # the derivation later without invalidating in-flight cookies
        # mid-deploy. SHA-256 is one-way; raw client_secret stays out of the
        # signer.
        derived_key = hashlib.sha256(
            b"iris-oauth-state-signing-v1:" + settings.client_secret.encode()
        ).digest()
        self._signer = URLSafeTimedSerializer(derived_key, salt="iris-oauth-state")
        # Lazy async-safe discovery: the first awaiter populates _discovered
        # and _jwks under _discovery_lock; subsequent callers see the
        # cached value. PyJWKClient bypasses httpx (uses urllib), so we
        # pre-load JWKS into a PyJWKSet ourselves.
        self._discovery_lock = asyncio.Lock()
        self._discovered: dict[str, Any] | None = None
        self._jwks: jwt.PyJWKSet | None = None

    async def _ensure_discovered(self) -> dict[str, Any]:
        if self._discovered is not None:
            return self._discovered
        async with self._discovery_lock:
            if self._discovered is not None:
                return self._discovered
            discovery_url = (
                self._settings.issuer_url.rstrip("/")
                + "/.well-known/openid-configuration"
            )
            try:
                doc_resp = await self._async_client.get(discovery_url)
                doc_resp.raise_for_status()
                doc = doc_resp.json()
                jwks_resp = await self._async_client.get(doc["jwks_uri"])
                jwks_resp.raise_for_status()
                jwks_doc = jwks_resp.json()
            except Exception as exc:
                logger.exception("auth: OIDC discovery failed")
                raise AuthError("oauth_discovery") from exc
            self._discovered = doc
            self._jwks = jwt.PyJWKSet.from_dict(jwks_doc)
            return doc

    async def close(self) -> None:
        """Close the async httpx client. Safe to call multiple times."""
        await self._async_client.aclose()

    async def begin(self, request: Request) -> Response:
        doc = await self._ensure_discovered()
        redirect_uri = str(request.url_for("login_callback"))
        url, state, verifier = self.build_authorize_url(
            redirect_uri=redirect_uri,
            authorize_endpoint=doc["authorization_endpoint"],
        )
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

    def build_authorize_url(
        self, *, redirect_uri: str, authorize_endpoint: str
    ) -> tuple[str, str, str]:
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
        return f"{authorize_endpoint}?{urlencode(params)}", state, verifier

    async def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> User:
        token_response = await self._request_tokens(
            code=code, code_verifier=code_verifier, redirect_uri=redirect_uri
        )
        id_token = token_response.get("id_token")
        if not id_token:
            logger.error("auth: token endpoint returned no id_token")
            raise AuthError("oauth_exchange")
        self._verify_id_token(id_token)
        try:
            access_token = token_response["access_token"]
        except KeyError as exc:
            logger.exception("auth: OAuth code exchange failed")
            raise AuthError("oauth_exchange") from exc
        claims = await self._fetch_userinfo(access_token)
        return self._user_from_claims(claims)

    async def _request_tokens(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> dict[str, Any]:
        doc = await self._ensure_discovered()
        try:
            r = await self._async_client.post(
                doc["token_endpoint"],
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
            return r.json()
        except Exception as exc:
            logger.exception("auth: OAuth code exchange failed")
            raise AuthError("oauth_exchange") from exc

    def _verify_id_token(self, id_token: str) -> None:
        # _verify_id_token is only reached after _request_tokens, which
        # awaits self._ensure_discovered() and populates _jwks. Guard
        # explicitly: a stripped ``assert`` (python -O) would skip the
        # signature-verification setup below.
        if self._jwks is None:
            raise AuthError("oauth_exchange")
        try:
            unverified_header = jwt.get_unverified_header(id_token)
            signing_key = self._jwks[unverified_header["kid"]].key
            jwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256", "ES256"],
                audience=self._settings.client_id,
                issuer=self._settings.issuer_url.rstrip("/"),
            )
        except (jwt.InvalidTokenError, KeyError) as exc:
            logger.exception("auth: id_token verification failed")
            raise AuthError("oauth_exchange") from exc

    async def _fetch_userinfo(self, access_token: str) -> dict[str, Any]:
        doc = await self._ensure_discovered()
        try:
            ui = await self._async_client.get(
                doc["userinfo_endpoint"],
                headers={"Authorization": f"Bearer {access_token}"},
            )
            ui.raise_for_status()
            return ui.json()
        except Exception as exc:
            logger.exception("auth: userinfo fetch failed")
            raise AuthError("oauth_exchange") from exc

    def _user_from_claims(self, claims: dict[str, Any]) -> User:
        groups = tuple(claims.get("groups") or ())
        if not groups:
            logger.warning(
                "auth: OAuth userinfo had no `groups` claim — check IdP client mapper"
            )
        sub = str(claims["sub"])
        username = str(claims.get("preferred_username") or sub)
        return User(
            subject=sub,
            username=username,
            display_name=str(claims.get("name") or username),
            groups=groups,
        )

    # OAuth provider has no .authenticate(username, password); the route layer
    # calls .begin() and .complete() instead.
