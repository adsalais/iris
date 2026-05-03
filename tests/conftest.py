import asyncio
import os

os.environ.setdefault("AUTH_METHOD", "mock")
os.environ.setdefault("MOCK_USERNAME", "alice")
os.environ.setdefault("MOCK_PASSWORD", "secret")
os.environ.setdefault("MOCK_GROUPS", "admins,users")
os.environ.setdefault("MOCK_DISPLAY_NAME", "Alice")
os.environ.setdefault("COOKIE_SECURE", "false")

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    from iris.app import build_app
    return build_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def authed_client(app):
    from iris.auth.identity import User

    c = TestClient(app)
    store = app.state.auth_session_store
    user = User(subject="mock:alice", username="alice", display_name="Alice", groups=("admins", "users"))
    session = asyncio.run(store.create(user))
    c.cookies.set("iris_session", session.id)
    return c
