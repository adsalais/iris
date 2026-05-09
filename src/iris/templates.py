"""Process-wide Jinja loader registry.

Each subsystem / feature ``install(app)`` calls
``register_template_dir(Path(__file__).parent / "templates")`` early in its
body. ``build_app()`` then calls ``init_templates()`` once after all
``install``s have run, and stashes the result on ``app.state.templates``.

First-registered wins on path collisions (FileSystemLoader default).
Subsystems should namespace their templates by directory
(``shell/shell.html``, ``auth/forbidden.html``, …) to avoid collisions.

Module-state is idempotent: re-registering the same path is a no-op,
and ``init_templates`` can be called multiple times — important because
``build_app`` is invoked per-test in the test suite.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_dirs: list[Path] = []


def register_template_dir(path: Path) -> None:
    """Append a template search dir. Idempotent — re-registering is a no-op."""
    resolved = Path(path)
    if resolved in _dirs:
        return
    _dirs.append(resolved)


def init_templates() -> Jinja2Templates:
    """Build the Jinja2Templates loader from the registered dirs.

    Can be called multiple times — each call returns a fresh loader over
    the current set of dirs. (build_app is invoked per-test.)
    """
    if not _dirs:
        msg = "no template directories registered"
        raise RuntimeError(msg)
    return Jinja2Templates(directory=_dirs)
