import pytest


def test_install_fails_loud_when_authz_config_path_missing(monkeypatch):
    """Without AUTHZ_CONFIG_PATH, build_app must fail at boot.

    AuthzSettings.from_env() reads os.environ at call time, so removing
    the variable for this one test is sufficient — no module reload needed.
    """
    from iris.app import build_app

    monkeypatch.delenv("AUTHZ_CONFIG_PATH", raising=False)

    with pytest.raises(ValueError, match="AUTHZ_CONFIG_PATH"):
        build_app()


def test_install_fails_loud_when_authz_yaml_invalid(tmp_path, monkeypatch):
    bad = tmp_path / "bad.yaml"
    bad.write_text("roles:\n  bad:\n    unknown_key: 1\n")
    monkeypatch.setenv("AUTHZ_CONFIG_PATH", str(bad))

    from iris.app import build_app
    from iris.auth.authz.mapping import RoleMappingError

    with pytest.raises(RoleMappingError):
        build_app()


def test_install_attaches_loader_to_app_state(tmp_path, monkeypatch):
    good = tmp_path / "good.yaml"
    good.write_text("roles:\n  reader: {}\n")
    monkeypatch.setenv("AUTHZ_CONFIG_PATH", str(good))

    from iris.app import build_app
    from iris.auth.authz.loader import RoleMappingLoader

    app = build_app()
    assert isinstance(app.state.authz_loader, RoleMappingLoader)
