import asyncio
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

# Test fixtures that the auth layer needs at import time. setdefault means
# a developer's real .env / shell env can still override these.
os.environ.setdefault("AUTH_METHOD", "mock")
os.environ.setdefault("MOCK_USERNAME", "alice")
os.environ.setdefault("MOCK_PASSWORD", "secret")
os.environ.setdefault("MOCK_GROUPS", "admins,users")
os.environ.setdefault("MOCK_DISPLAY_NAME", "Alice")
os.environ.setdefault("COOKIE_SECURE", "false")
# Tests don't want the CH bridge installed by default (auth tests don't need
# a CH testcontainer). Bridge tests opt in via build_app(install_clickhouse=True).
os.environ.setdefault("IRIS_NO_CLICKHOUSE", "1")
# Sessions are stored in SQLite. One connection per process means :memory:
# works for single-process tests; multi-process tests use a tempfile.
os.environ.setdefault("AUTH_DB_PATH", ":memory:")

# Write a fixture role mapping that maps the mock user's groups into roles
# so authed_client can hit role-gated routes. Lives in a tempfile that's
# not cleaned up — leaks one file per test session, acceptable for v1.
_AUTHZ_FIXTURE = """\
roles:
  reader:
    groups: []
    users: []
  writer:
    groups: []
    users: []
    includes: ["reader"]
  admin:
    groups: ["admins"]
    users: []
    includes: ["writer"]
  clickhouse_admin:
    groups: ["admins"]
    users: []
"""

_authz_path = os.path.join(tempfile.gettempdir(), "iris-test-authz.yaml")
with open(_authz_path, "w") as f:
    f.write(_AUTHZ_FIXTURE)
os.environ.setdefault("AUTHZ_CONFIG_PATH", _authz_path)


@pytest.fixture
def app():
    from iris.app import build_app

    return build_app(install_clickhouse=False)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def authed_client(app):
    from iris.auth.identity import User

    c = TestClient(app)
    store = app.state.auth_session_store
    user = User(
        subject="mock:alice",
        username="alice",
        display_name="Alice",
        groups=("admins", "users"),
    )
    session = asyncio.run(store.create(user))
    c.cookies.set("iris_session", session.id)
    return c
