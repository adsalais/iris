"""Process-wide Jinja loader registry, prefix-namespaced.

Each subsystem / feature ``install(app)`` calls
``register_template_dir(prefix, Path(__file__).parent / "templates")``.
``build_app()`` then calls ``init_templates()`` once after all installs
have run. The result is a ``Jinja2Templates`` whose env uses a
``PrefixLoader`` mapping each registered prefix to its templates dir.

Lookup strings in code are ``<prefix>/<name>.html`` — same as before;
the prefix used to be a directory level inside each ``templates/`` dir,
now it's the registration-time argument.

Re-registering the same (prefix, path) pair is idempotent (covers the
case where ``build_app`` is called per-test). Re-registering the same
prefix with a DIFFERENT path raises ``ValueError`` so typos at startup
fail loudly instead of silently shadowing.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, PrefixLoader, select_autoescape

_prefixed_dirs: dict[str, Path] = {}


def register_template_dir(prefix: str, path: Path) -> None:
    """Register `path` to serve templates under the ``<prefix>/...`` namespace."""
    resolved = Path(path)
    existing = _prefixed_dirs.get(prefix)
    if existing == resolved:
        return  # idempotent
    if existing is not None:
        msg = (
            f"prefix {prefix!r} already registered with path {existing!r}; "
            f"cannot re-register with {resolved!r}"
        )
        raise ValueError(msg)
    _prefixed_dirs[prefix] = resolved


def init_templates() -> Jinja2Templates:
    """Build a Jinja2Templates with a PrefixLoader over registered dirs.

    Lookup strings of the form ``<prefix>/<name>.html`` resolve to
    ``<dir>/<name>.html`` for the matching prefix. Templates that include
    other templates by ``<prefix>/<name>.html`` continue to work because
    the PrefixLoader applies to all loads.
    """
    if not _prefixed_dirs:
        msg = "no template directories registered"
        raise RuntimeError(msg)
    env = Environment(
        loader=PrefixLoader({
            prefix: FileSystemLoader(str(path))
            for prefix, path in _prefixed_dirs.items()
        }),
        autoescape=select_autoescape(["html", "htm", "xml"]),
    )
    return Jinja2Templates(env=env)
