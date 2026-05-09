from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_templates_registry() -> Iterator[None]:
    """templates._prefixed_dirs is module state; reset between tests."""
    import iris.templates
    importlib.reload(iris.templates)
    yield
    importlib.reload(iris.templates)


def test_register_template_dir_resolves_via_prefix(tmp_path: Path):
    import iris.templates as t
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "a.html").write_text("from d1")
    (d2 / "b.html").write_text("from d2")
    t.register_template_dir("p1", d1)
    t.register_template_dir("p2", d2)
    templates = t.init_templates()
    assert templates.get_template("p1/a.html").render() == "from d1"
    assert templates.get_template("p2/b.html").render() == "from d2"


def test_init_templates_with_no_dirs_raises():
    import iris.templates as t
    with pytest.raises(RuntimeError, match="no template directories registered"):
        t.init_templates()


def test_register_idempotent_with_same_prefix_and_path(tmp_path: Path):
    import iris.templates as t
    d = tmp_path / "d"
    d.mkdir()
    t.register_template_dir("p", d)
    t.register_template_dir("p", d)
    t.register_template_dir("p", d)
    # Same (prefix, path) pair across multiple calls is a no-op.
    assert t._prefixed_dirs == {"p": d.resolve()}
    t.init_templates()
    t.register_template_dir("p", d)  # still a no-op after init


def test_register_different_path_for_same_prefix_raises(tmp_path: Path):
    import iris.templates as t
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    t.register_template_dir("p", d1)
    with pytest.raises(ValueError, match="prefix 'p' already registered"):
        t.register_template_dir("p", d2)
