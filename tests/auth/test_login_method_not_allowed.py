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


def test_safe_next_rejects_crlf():
    """CRLF in `next` would otherwise enable header injection through the
    Location response header."""
    from iris.auth.routes import _safe_next
    assert _safe_next("/foo\r\nSet-Cookie: x=y") == "/"
    assert _safe_next("/foo\nbar") == "/"
    assert _safe_next("/foo\rbar") == "/"


def test_safe_next_rejects_backslash():
    """Pre-existing rejection: browsers normalize \\ to / in URLs."""
    from iris.auth.routes import _safe_next
    assert _safe_next("/\\evil") == "/"


def test_safe_next_rejects_protocol_relative():
    from iris.auth.routes import _safe_next
    assert _safe_next("//evil.example.com/path") == "/"


def test_safe_next_rejects_absolute():
    from iris.auth.routes import _safe_next
    assert _safe_next("https://evil.example.com/path") == "/"


def test_safe_next_accepts_relative_path():
    from iris.auth.routes import _safe_next
    assert _safe_next("/dashboard") == "/dashboard"


def test_safe_next_logs_info_on_rejection(caplog):
    """U5: every rewrite-to-/ branch logs at INFO with reason= and a
    truncated next= value."""
    import logging

    from iris.auth.routes import _safe_next

    caplog.set_level(logging.INFO, logger="iris.auth")
    _safe_next("/x\r\ny")
    assert any(
        "safe_next_rejected" in record.message and "reason=crlf" in record.message
        for record in caplog.records
    ), [r.message for r in caplog.records]

    caplog.clear()
    _safe_next("//evil")
    assert any(
        "safe_next_rejected" in record.message and "reason=non_relative" in record.message
        for record in caplog.records
    ), [r.message for r in caplog.records]


def test_safe_next_truncates_logged_value(caplog):
    """Defense against log injection via giant next= payloads."""
    import logging

    from iris.auth.routes import _safe_next

    caplog.set_level(logging.INFO, logger="iris.auth")
    # CRLF-prefixed long string is surely rejected.
    _safe_next("\r" + "A" * 10_000)
    rejected_messages = [r.getMessage() for r in caplog.records if "safe_next_rejected" in r.getMessage()]
    assert rejected_messages, [r.getMessage() for r in caplog.records]
    for msg in rejected_messages:
        assert len(msg) < 500, f"log message not truncated: {len(msg)} chars"
