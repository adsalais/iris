import logging
import time

import pytest

from iris.auth.authz.loader import RoleMappingLoader
from iris.auth.authz.mapping import RoleMapping, RoleMappingError


_VALID = """
roles:
  reader:
    groups: ["readers"]
"""

_VALID_2 = """
roles:
  writer:
    groups: ["writers"]
"""


def _write(path, text):
    """Write text and bump mtime by 1s to ensure st_mtime_ns changes."""
    path.write_text(text)
    # On some filesystems (e.g., older ext4 without nanosecond precision)
    # consecutive writes within the same second can leave mtime unchanged.
    # Bump it explicitly.
    new_t = time.time() + 1
    import os
    os.utime(path, (new_t, new_t))


def test_initial_load_returns_parsed_mapping(tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    m = loader.get()
    assert isinstance(m, RoleMapping)
    assert "reader" in m.roles


def test_cached_read_does_not_reparse(tmp_path, monkeypatch):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()

    # Spy on parse to confirm it isn't called again on the second get()
    from iris.auth.authz import loader as loader_mod
    calls = {"n": 0}
    real_parse = loader_mod.parse

    def counting_parse(text):
        calls["n"] += 1
        return real_parse(text)

    monkeypatch.setattr(loader_mod, "parse", counting_parse)

    second = loader.get()
    assert second is first
    assert calls["n"] == 0


def test_mtime_change_triggers_reload(tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()
    assert "reader" in first.roles

    _write(p, _VALID_2)
    second = loader.get()
    assert "writer" in second.roles
    assert "reader" not in second.roles
    assert second is not first


def test_invalid_edit_after_good_load_returns_last_good(tmp_path, caplog):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()

    _write(p, "roles:\n  bad:\n    unknown_key: 1\n")

    with caplog.at_level(logging.ERROR, logger="iris.auth.authz.loader"):
        second = loader.get()

    assert second is first  # last-good fallback
    assert any("authz" in rec.message.lower() or "role" in rec.message.lower() for rec in caplog.records)


def test_deleted_file_after_good_load_returns_last_good(tmp_path, caplog):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()

    p.unlink()

    with caplog.at_level(logging.ERROR, logger="iris.auth.authz.loader"):
        second = loader.get()

    assert second is first


def test_first_load_failure_raises(tmp_path):
    p = tmp_path / "missing.yaml"
    loader = RoleMappingLoader(p)
    with pytest.raises((FileNotFoundError, RoleMappingError)):
        loader.get()


def test_first_load_invalid_content_raises(tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text("roles:\n  bad:\n    unknown_key: 1\n")
    loader = RoleMappingLoader(p)
    with pytest.raises(RoleMappingError):
        loader.get()


def test_recovery_after_bad_then_good(tmp_path):
    p = tmp_path / "authz.yaml"
    p.write_text(_VALID)
    loader = RoleMappingLoader(p)
    first = loader.get()

    _write(p, "garbage: [unclosed")
    bad = loader.get()
    assert bad is first  # fallback

    _write(p, _VALID_2)
    recovered = loader.get()
    assert "writer" in recovered.roles
    assert recovered is not first
