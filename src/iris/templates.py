"""Process-wide Jinja loader registry.

Each subsystem / feature ``install(app)`` calls
``register_template_dir(Path(__file__).parent / "templates")`` early in its
body. ``build_app()`` then calls ``init_templates()`` once after all
``install``s have run, and stashes the result on ``app.state.templates``.

First-registered wins on path collisions (FileSystemLoader default).
Subsystems should namespace their templates by directory
(``shell/shell.html``, ``auth/forbidden.html``, …) to avoid collisions.

Backward shim: existing callers (``iris.app``, ``iris.auth.routes.install``)
import ``TEMPLATES`` at module-import time. Until those callers migrate to
the ``init_templates`` pattern (Task 1.10 in the frontend-architecture plan),
the ``TEMPLATES`` symbol is a ``_LazyTemplates`` shim that pre-registers the
legacy directory on first access. The shim is removed in Task 1.10 once all
callers move.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

_dirs: list[Path] = []
_initialized: bool = False


def register_template_dir(path: Path) -> None:
    """Append a template search dir. Must be called before ``init_templates``."""
    if _initialized:
        msg = (
            "iris.templates already initialized; register_template_dir "
            + "must be called before init_templates"
        )
        raise RuntimeError(msg)
    _dirs.append(path)


def init_templates() -> Jinja2Templates:
    """Build the Jinja2Templates loader from the registered dirs."""
    global _initialized
    if not _dirs:
        msg = "no template directories registered"
        raise RuntimeError(msg)
    _initialized = True
    return Jinja2Templates(directory=_dirs)


def _legacy_default() -> Jinja2Templates:
    if not _dirs:
        register_template_dir(Path(__file__).parent / "templates")
    return init_templates()


class _LazyTemplates:
    """Stand-in that defers to init_templates on first attribute access.

    Removed in Task 1.10 of the frontend-architecture plan once all callers
    have migrated to the registry pattern.
    """
    _real: Jinja2Templates | None = None

    def _resolve(self) -> Jinja2Templates:
        if self._real is None:
            self._real = _legacy_default()
        return self._real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)


TEMPLATES: Any = _LazyTemplates()
