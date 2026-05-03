from pathlib import Path

import pytest

from iris.auth.authz.config import AuthzSettings


def test_from_env_reads_path(monkeypatch, tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text("roles: {}\n")
    monkeypatch.setenv("AUTHZ_CONFIG_PATH", str(p))
    s = AuthzSettings.from_env()
    assert s.config_path == p
    assert isinstance(s.config_path, Path)


def test_from_env_rejects_missing_var(monkeypatch):
    monkeypatch.delenv("AUTHZ_CONFIG_PATH", raising=False)
    with pytest.raises(ValueError, match="AUTHZ_CONFIG_PATH"):
        AuthzSettings.from_env()


def test_from_env_rejects_empty_var(monkeypatch):
    monkeypatch.setenv("AUTHZ_CONFIG_PATH", "   ")
    with pytest.raises(ValueError, match="AUTHZ_CONFIG_PATH"):
        AuthzSettings.from_env()
