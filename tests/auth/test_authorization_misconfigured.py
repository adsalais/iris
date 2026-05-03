from fastapi import FastAPI
from fastapi.testclient import TestClient

from iris.auth.exceptions import AuthorizationMisconfigured, install_exception_handlers


def test_authorization_misconfigured_returns_500_without_leaking_role_name(caplog):
    import logging

    app = FastAPI()
    install_exception_handlers(app, cookie_name="iris_session")

    @app.get("/oops")
    async def oops():
        raise AuthorizationMisconfigured("super_admin")

    with caplog.at_level(logging.ERROR, logger="iris.auth"):
        r = TestClient(app).get("/oops")

    assert r.status_code == 500
    assert "super_admin" not in r.text  # role name must not leak in response body
    # but it should appear in logs so operators can find the misconfig
    assert any("super_admin" in rec.message for rec in caplog.records)


def test_authorization_misconfigured_constructor_stores_role_name():
    exc = AuthorizationMisconfigured("missing_role")
    assert exc.role == "missing_role"
    assert "missing_role" in str(exc)
