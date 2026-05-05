import os
import pytest

from iris.clickhouse.config import ClickHouseSettings


@pytest.fixture
def env(monkeypatch):
    """Wipe and rebuild the CLICKHOUSE_* env so tests are hermetic."""
    for key in list(os.environ):
        if key.startswith("CLICKHOUSE_"):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_from_env_minimal_happy_path(env):
    env.setenv("CLICKHOUSE_HOST", "ch.example.com")
    env.setenv("CLICKHOUSE_PORT", "8443")
    env.setenv("CLICKHOUSE_USER", "iris_service")
    env.setenv("CLICKHOUSE_PASSWORD", "secret")
    env.setenv("CLICKHOUSE_SECURE", "true")
    env.setenv("CLICKHOUSE_VERIFY", "true")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "iris_service")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "service_admin_role")

    s = ClickHouseSettings.from_env()

    assert s.host == "ch.example.com"
    assert s.port == 8443
    assert s.user == "iris_service"
    assert s.password == "secret"
    assert s.secure is True
    assert s.verify is True
    assert s.ca_cert_path is None
    assert s.service_admin_user == "iris_service"
    assert s.service_admin_role == "service_admin_role"


def test_from_env_optional_ca_cert_path(env):
    env.setenv("CLICKHOUSE_HOST", "h")
    env.setenv("CLICKHOUSE_PORT", "9000")
    env.setenv("CLICKHOUSE_USER", "u")
    env.setenv("CLICKHOUSE_PASSWORD", "p")
    env.setenv("CLICKHOUSE_SECURE", "false")
    env.setenv("CLICKHOUSE_VERIFY", "false")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_USER", "u")
    env.setenv("CLICKHOUSE_SERVICE_ADMIN_ROLE", "r")
    env.setenv("CLICKHOUSE_CA_CERT_PATH", "/etc/ssl/ca.pem")

    s = ClickHouseSettings.from_env()

    assert s.ca_cert_path == "/etc/ssl/ca.pem"
    assert s.secure is False
    assert s.verify is False
