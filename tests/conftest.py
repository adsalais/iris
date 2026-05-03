import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    from iris.app import build_app
    return build_app()


@pytest.fixture
def client(app):
    return TestClient(app)
