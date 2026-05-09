from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def reset_templates_registry() -> Iterator[None]:
    """templates._dirs is module-state; reset between tests."""
    import iris.templates
    importlib.reload(iris.templates)
    yield
    importlib.reload(iris.templates)


def test_register_template_dir_appends_to_search_path(tmp_path: Path):
    import iris.templates as t
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "a.html").write_text("from d1")
    (d2 / "b.html").write_text("from d2")
    t.register_template_dir(d1)
    t.register_template_dir(d2)
    templates = t.init_templates()
    assert templates.get_template("a.html").render() == "from d1"
    assert templates.get_template("b.html").render() == "from d2"


def test_init_templates_with_no_dirs_raises():
    import iris.templates as t
    with pytest.raises(RuntimeError, match="no template directories registered"):
        t.init_templates()


def test_register_template_dir_after_init_raises(tmp_path: Path):
    import iris.templates as t
    d = tmp_path / "d"
    d.mkdir()
    t.register_template_dir(d)
    t.init_templates()
    with pytest.raises(RuntimeError, match="already initialized"):
        t.register_template_dir(d)


def test_first_match_wins_when_paths_collide(tmp_path: Path):
    import iris.templates as t
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    (d1 / "x.html").write_text("d1 wins")
    (d2 / "x.html").write_text("d2 loses")
    t.register_template_dir(d1)
    t.register_template_dir(d2)
    templates = t.init_templates()
    assert templates.get_template("x.html").render() == "d1 wins"
