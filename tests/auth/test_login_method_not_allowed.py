"""When AUTH_METHOD=oauth, POST /login is not allowed; the 405 response
must include an ``Allow`` header per RFC 7231 §6.5.5.

The CSRF dep runs before the body of login_post, so a fresh GET is
needed to seed the cookie + token before the POST can reach the
provider-type check.
"""
import pytest
from fastapi.testclient import TestClient

from iris.auth.csrf import CSRF_COOKIE_NAME, CSRF_FORM_FIELD


def test_login_post_returns_405_with_allow_header(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://kc.example/realms/iris")
    monkeypatch.setenv("OIDC_CLIENT_ID", "iris")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("AUTH_DB_PATH", ":memory:")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    from iris.app import build_app

    app = build_app(install_clickhouse=False)
    with TestClient(app) as client:
        # Seed a CSRF cookie via any GET that sets it. The mock OAuth provider
        # would actually redirect on /login, so we mint one directly through
        # the helper to avoid hitting the real IdP.
        token = "A" * 32  # well-formed urlsafe-base64 token
        client.cookies.set(CSRF_COOKIE_NAME, token)
        response = client.post(
            "/login",
            data={
                "username": "x",
                "password": "y",
                CSRF_FORM_FIELD: token,
            },
        )
    assert response.status_code == 405, (
        f"expected 405, got {response.status_code}: {response.text}"
    )
    assert response.headers.get("Allow") == "GET"
