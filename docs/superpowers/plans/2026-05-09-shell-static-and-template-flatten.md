# Shell static + template flattening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) Move `datastar.js` to the shell's static dir and drop the global `/static` mount; (2) flatten the `<module>/templates/<module>/<name>.html` double-nested directory layout via Jinja `PrefixLoader`, with prefixes declared at registration time.

**Architecture:** Two independent commits. Task 1 is the small `datastar.js` move (file + 4 small edits). Task 2 is the templates refactor (new `register_template_dir(prefix, path)` signature + `PrefixLoader`-backed `Jinja2Templates(env=...)` + 19 file moves + ~11 call site updates). Lookup strings in code (`"auth/forbidden.html"`, etc.) are unchanged in both tasks.

**Tech Stack:** Python 3.13, Starlette `Jinja2Templates(env=...)` (verified to accept a custom `Environment` with `PrefixLoader`), Jinja2 `PrefixLoader` + `FileSystemLoader` + `select_autoescape`. No new runtime deps.

---

## Pre-plan verification

Verified at plan-write time: `Jinja2Templates(env=Environment(loader=PrefixLoader({...}), autoescape=...))` constructs successfully and `templates.get_template("auth/forbidden.html")` resolves correctly via the prefix. Starlette ≥ 0.30 supports the `env=` constructor kwarg.

---

## File map

### Moved files (no content change unless noted)

**Task 1 — datastar.js:**

| From | To |
|---|---|
| `src/iris/static/datastar.js` | `src/iris/shell/static/datastar.js` |

**Task 2 — templates (drop the inner prefix dir):**

| From | To |
|---|---|
| `src/iris/auth/templates/auth/forbidden.html` | `src/iris/auth/templates/forbidden.html` |
| `src/iris/auth/templates/auth/ldap_form.html` | `src/iris/auth/templates/ldap_form.html` |
| `src/iris/shell/templates/shell/shell.html` | `src/iris/shell/templates/shell.html` |
| `src/iris/shell/templates/shell/_tab_panel.html` | `src/iris/shell/templates/_tab_panel.html` |
| `src/iris/shell/templates/shell/_tab_strip.html` | `src/iris/shell/templates/_tab_strip.html` |
| `src/iris/shell/templates/shell/_account_popover.html` | `src/iris/shell/templates/_account_popover.html` |
| `src/iris/shell/templates/shell/_top_buttons.html` | `src/iris/shell/templates/_top_buttons.html` |
| `src/iris/features/authorization/templates/authorization/my_access.html` | `src/iris/features/authorization/templates/my_access.html` |
| `src/iris/features/authorization/templates/authorization/manage.html` | `src/iris/features/authorization/templates/manage.html` |
| `src/iris/features/authorization/templates/authorization/_members_section.html` | `src/iris/features/authorization/templates/_members_section.html` |
| `src/iris/features/authorization/templates/authorization/_row_policies.html` | `src/iris/features/authorization/templates/_row_policies.html` |
| `src/iris/features/authorization/templates/authorization/_audit.html` | `src/iris/features/authorization/templates/_audit.html` |
| `src/iris/features/authorization/templates/authorization/_danger.html` | `src/iris/features/authorization/templates/_danger.html` |
| `src/iris/features/authorization/templates/authorization/create_database.html` | `src/iris/features/authorization/templates/create_database.html` |
| `src/iris/features/authorization/templates/authorization/admin_console.html` | `src/iris/features/authorization/templates/admin_console.html` |
| `src/iris/features/authorization/templates/authorization/_admin_users.html` | `src/iris/features/authorization/templates/_admin_users.html` |
| `src/iris/features/authorization/templates/authorization/_admin_databases.html` | `src/iris/features/authorization/templates/_admin_databases.html` |
| `src/iris/features/authorization/templates/authorization/_admin_policies.html` | `src/iris/features/authorization/templates/_admin_policies.html` |
| `src/iris/features/authorization/templates/authorization/_admin_audit.html` | `src/iris/features/authorization/templates/_admin_audit.html` |

### Modified files

**Task 1:**

| Path | Change |
|---|---|
| `src/iris/app.py` | Delete the global `app.mount("/static", ...)` block; remove the `Path` and `StaticFiles` imports if no longer used |
| `src/iris/shell/templates/shell/shell.html` | `<script src="/static/datastar.js">` → `<script src="/static/shell/datastar.js">` |
| `tests/test_static_assets.py` | URLs `/static/datastar.js` → `/static/shell/datastar.js`; `/static/does-not-exist.js` → `/static/shell/does-not-exist.js` |

**Task 2:**

| Path | Change |
|---|---|
| `src/iris/templates.py` | `register_template_dir` gains `prefix: str`; `init_templates` builds `Jinja2Templates(env=Environment(loader=PrefixLoader(...), autoescape=...))` |
| `src/iris/auth/routes.py` | `register_template_dir(...)` → `register_template_dir("auth", ...)` |
| `src/iris/shell/install.py` | `register_template_dir(...)` → `register_template_dir("shell", ...)` |
| `src/iris/features/authorization/install.py` | `register_template_dir(...)` → `register_template_dir("authorization", ...)` |
| `tests/auth/test_exception_handler.py` | `register_template_dir(...)` → `register_template_dir("auth", ...)` (1 site) |
| `tests/auth/test_provider_oauth.py` | Same (1 site) |
| `tests/auth/test_provider_mock.py` | Same (5 sites) |
| `tests/shell/test_templates_loader.py` | All 4 existing tests updated for the new signature; `test_first_match_wins_when_paths_collide` is rewritten as `test_register_different_path_for_same_prefix_raises`; new `test_register_idempotent_with_same_prefix_and_path` |

### Deleted directories (after their files have been moved)

| Path | When |
|---|---|
| `src/iris/static/` (now empty) | Task 1 |
| `src/iris/auth/templates/auth/` (now empty) | Task 2 |
| `src/iris/shell/templates/shell/` (now empty) | Task 2 |
| `src/iris/features/authorization/templates/authorization/` (now empty) | Task 2 |

---

## Task 1 — Move `datastar.js` to shell, drop the global `/static` mount

**Files:**
- Move: `src/iris/static/datastar.js` → `src/iris/shell/static/datastar.js`
- Modify: `src/iris/app.py`
- Modify: `src/iris/shell/templates/shell/shell.html`
- Modify: `tests/test_static_assets.py`
- Delete: `src/iris/static/` (empty after the move)

- [ ] **Step 1: Snapshot the test baseline**

```bash
uv run pytest tests/test_static_assets.py -v
```
Expected: 2 tests pass at the current commit.

- [ ] **Step 2: Move `datastar.js` to the shell's static dir**

```bash
git mv src/iris/static/datastar.js src/iris/shell/static/datastar.js
rmdir src/iris/static
```

If `rmdir` errors with "not empty," investigate (something else is in there that the spec didn't anticipate) — abort and ask. Expected: the directory has only `datastar.js`, so `rmdir` succeeds after the move.

- [ ] **Step 3: Update the script tag in `shell.html`**

In `src/iris/shell/templates/shell/shell.html`, find:

```html
  <script type="module" src="/static/datastar.js"></script>
```

Change to:

```html
  <script type="module" src="/static/shell/datastar.js"></script>
```

(The file moves to `src/iris/shell/templates/shell.html` in Task 2, but for now it's still at the nested location — edit it in place.)

- [ ] **Step 4: Delete the global `/static` mount in `iris.app.build_app`**

In `src/iris/app.py`, find and DELETE this block:

```python
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
```

Then drop now-unused imports if any. Currently `app.py` imports `Path` and `StaticFiles` only for this mount, so:

```python
# Delete:
from pathlib import Path
from fastapi.staticfiles import StaticFiles
```

After the deletions, the imports at the top of `app.py` should look like:

```python
from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from iris.middleware import SecurityHeadersMiddleware
from iris.templates import init_templates
```

- [ ] **Step 5: Update `tests/test_static_assets.py` URLs**

Replace the file's contents:

```python
"""Static-files mount serves the vendored Datastar bundle (now under shell)."""
from fastapi.testclient import TestClient

from iris.app import build_app


def test_static_datastar_js_is_served():
    app = build_app(install_clickhouse=False)
    c = TestClient(app)
    r = c.get("/static/shell/datastar.js")
    assert r.status_code == 200
    ct = r.headers.get("content-type", "")
    assert ct.startswith(("application/javascript", "text/javascript")), (
        f"unexpected content-type: {ct!r}"
    )
    # Sanity-check the body: real bundle, not a stub or HTML 404 page.
    assert len(r.content) > 10_000, f"datastar.js body too small ({len(r.content)} bytes)"
    # The bundle is plain JS source, must decode as UTF-8 cleanly.
    r.content.decode("utf-8")  # raises UnicodeDecodeError on failure


def test_shell_static_mount_404s_for_missing_file():
    app = build_app(install_clickhouse=False)
    c = TestClient(app)
    r = c.get("/static/shell/does-not-exist.js")
    assert r.status_code == 404
```

- [ ] **Step 6: Run tests + gates**

```bash
uv run pytest tests/test_static_assets.py -v
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: all pass; the renamed test name (`test_shell_static_mount_404s_for_missing_file`) reflects the new mount.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(shell): move datastar.js into shell/static; drop global /static mount

Datastar is the shell's runtime — only shell.html loads it. Moving the
file to src/iris/shell/static/ and serving it via the existing
/static/shell/ mount removes the holdover global /static mount in
build_app and makes shell self-contained.

Files:
- git mv src/iris/static/datastar.js src/iris/shell/static/datastar.js
- shell.html script src: /static/datastar.js → /static/shell/datastar.js
- app.py: delete the app.mount("/static", ...) block + the Path /
  StaticFiles imports it required
- test_static_assets.py: URLs updated to /static/shell/...

The empty src/iris/static/ directory is removed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — `register_template_dir(prefix, path)` + flatten templates

**Files:** see file map above. ~30 file moves/edits across one commit. Single coherent transformation; intermediate states would leave the system broken.

- [ ] **Step 1: Snapshot the baseline**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
```
Expected: 558 tests pass at the current commit (or 559 if Task 1's `test_static_assets.py` rename added one — keep the count for the post-Task-2 comparison).

- [ ] **Step 2: Rewrite `src/iris/templates.py` with the new signature + PrefixLoader-backed env**

Replace the file's contents:

```python
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
```

(`Jinja2Templates(env=...)` is supported in Starlette ≥ 0.30; verified at plan-write time.)

- [ ] **Step 3: Update the 3 install call sites**

In `src/iris/auth/routes.py`, find the call inside the `install` function:

```python
    register_template_dir(Path(__file__).parent / "templates")
```

Change to:

```python
    register_template_dir("auth", Path(__file__).parent / "templates")
```

In `src/iris/shell/install.py`, find:

```python
    register_template_dir(Path(__file__).parent / "templates")
```

Change to:

```python
    register_template_dir("shell", Path(__file__).parent / "templates")
```

In `src/iris/features/authorization/install.py`, find:

```python
    register_template_dir(Path(__file__).parent / "templates")
```

Change to:

```python
    register_template_dir("authorization", Path(__file__).parent / "templates")
```

- [ ] **Step 4: Update the 7 test call sites**

For each of the following call sites, the existing line:

```python
    register_template_dir(Path(iris.__file__).parent / "auth" / "templates")
```

becomes:

```python
    register_template_dir("auth", Path(iris.__file__).parent / "auth" / "templates")
```

A precise sed replacement (verify with grep before & after):

```bash
sed -i \
  's|register_template_dir(Path(iris.__file__).parent / "auth" / "templates")|register_template_dir("auth", Path(iris.__file__).parent / "auth" / "templates")|g' \
  tests/auth/test_exception_handler.py tests/auth/test_provider_mock.py tests/auth/test_provider_oauth.py
```

Verify:

```bash
grep -rn "register_template_dir" tests/auth/
```
Expected: every line has `register_template_dir("auth", ...)`.

- [ ] **Step 5: Move 19 template files up one level + delete empty subdirs**

```bash
# auth: 2 files
git mv src/iris/auth/templates/auth/forbidden.html src/iris/auth/templates/forbidden.html
git mv src/iris/auth/templates/auth/ldap_form.html src/iris/auth/templates/ldap_form.html
rmdir src/iris/auth/templates/auth

# shell: 5 files
git mv src/iris/shell/templates/shell/shell.html src/iris/shell/templates/shell.html
git mv src/iris/shell/templates/shell/_tab_panel.html src/iris/shell/templates/_tab_panel.html
git mv src/iris/shell/templates/shell/_tab_strip.html src/iris/shell/templates/_tab_strip.html
git mv src/iris/shell/templates/shell/_account_popover.html src/iris/shell/templates/_account_popover.html
git mv src/iris/shell/templates/shell/_top_buttons.html src/iris/shell/templates/_top_buttons.html
rmdir src/iris/shell/templates/shell

# authorization: 12 files
git mv src/iris/features/authorization/templates/authorization/my_access.html src/iris/features/authorization/templates/my_access.html
git mv src/iris/features/authorization/templates/authorization/manage.html src/iris/features/authorization/templates/manage.html
git mv src/iris/features/authorization/templates/authorization/_members_section.html src/iris/features/authorization/templates/_members_section.html
git mv src/iris/features/authorization/templates/authorization/_row_policies.html src/iris/features/authorization/templates/_row_policies.html
git mv src/iris/features/authorization/templates/authorization/_audit.html src/iris/features/authorization/templates/_audit.html
git mv src/iris/features/authorization/templates/authorization/_danger.html src/iris/features/authorization/templates/_danger.html
git mv src/iris/features/authorization/templates/authorization/create_database.html src/iris/features/authorization/templates/create_database.html
git mv src/iris/features/authorization/templates/authorization/admin_console.html src/iris/features/authorization/templates/admin_console.html
git mv src/iris/features/authorization/templates/authorization/_admin_users.html src/iris/features/authorization/templates/_admin_users.html
git mv src/iris/features/authorization/templates/authorization/_admin_databases.html src/iris/features/authorization/templates/_admin_databases.html
git mv src/iris/features/authorization/templates/authorization/_admin_policies.html src/iris/features/authorization/templates/_admin_policies.html
git mv src/iris/features/authorization/templates/authorization/_admin_audit.html src/iris/features/authorization/templates/_admin_audit.html
rmdir src/iris/features/authorization/templates/authorization
```

Verify the directory tree:

```bash
find src/iris/auth/templates src/iris/shell/templates src/iris/features/authorization/templates -type f
```
Expected: 19 files, all directly under their `templates/` dir (no inner prefix subdirectory).

- [ ] **Step 6: Rewrite `tests/shell/test_templates_loader.py` for the new contract**

Replace the file's contents:

```python
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
```

Behavior changes from the old test file: `test_first_match_wins_when_paths_collide` is gone (PrefixLoader has no first-wins concept; collisions on the same prefix raise instead). The four tests above cover: prefix resolution, no-dirs error, idempotency, duplicate-prefix rejection.

- [ ] **Step 7: Run the affected test files individually to catch easy mistakes**

```bash
uv run pytest tests/shell/test_templates_loader.py tests/auth/test_exception_handler.py tests/auth/test_provider_mock.py tests/auth/test_provider_oauth.py tests/test_app.py -v 2>&1 | tail -30
```
Expected: all pass. If any test errors with "TemplateNotFound: '<prefix>/<name>.html'", the file at the new path is missing or the prefix is wrong — verify the moves and the registration call signatures.

- [ ] **Step 8: Run the full unit suite + gates**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: same test count as the pre-Task-2 baseline (the `test_first_match_wins_when_paths_collide` test is gone, but `test_register_idempotent_with_same_prefix_and_path` and `test_register_different_path_for_same_prefix_raises` together provide net zero or +1 — count one more than baseline since the old file had 4 tests and the new has 4 too). Ruff + basedpyright clean.

If a test fails with `TemplateNotFound`, the symptom is the lookup string not matching the registered prefix → file path mapping. Check:

1. The test's `register_template_dir(prefix, path)` call has `prefix` matching the lookup string's first segment.
2. The file actually exists at `<path>/<name>.html` (NOT `<path>/<prefix>/<name>.html`).

- [ ] **Step 9: Optional integration regression**

```bash
uv run pytest tests/clickhouse/integration tests/auth/integration -q
```
Expected: 23 integration tests pass.

- [ ] **Step 10: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(templates): PrefixLoader-based registration; flatten <module>/templates/<module>/

register_template_dir(path) becomes register_template_dir(prefix, path).
init_templates() builds a Jinja2Templates(env=Environment(loader=
PrefixLoader({prefix: FileSystemLoader(path), ...}), autoescape=
select_autoescape(...))).

Lookup strings in code (templates.TemplateResponse(request,
"auth/forbidden.html", ...)) are unchanged. Only the directory layout
and the registration signature change: each module's templates live
directly under its templates/ dir (no inner <prefix>/ subdir), and the
prefix is declared at registration time.

19 template files move up one level (drop the inner prefix dir):

  src/iris/auth/templates/auth/{forbidden,ldap_form}.html → ../*.html
  src/iris/shell/templates/shell/{shell,_tab_panel,_tab_strip,
    _account_popover,_top_buttons}.html → ../*.html
  src/iris/features/authorization/templates/authorization/*.html (12) →
    ../*.html

Three now-empty <prefix>/ subdirectories are removed.

Behavior change: re-registering the same prefix with a DIFFERENT path
now raises ValueError (was: silently picked the first registered path
under the old multi-FileSystemLoader semantics). Re-registering the
same (prefix, path) pair is still idempotent.

The four tests in tests/shell/test_templates_loader.py are rewritten
for the new contract: prefix resolution, no-dirs error, idempotency,
duplicate-prefix rejection. test_first_match_wins_when_paths_collide
is gone (the concept doesn't exist under PrefixLoader).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Recap

2 tasks, 2 commits. End state:

- `datastar.js` lives at `src/iris/shell/static/datastar.js`; served at `/static/shell/datastar.js`. Global `/static` mount is gone. `src/iris/static/` directory is gone.
- Each module's templates live directly under `<module>/templates/<name>.html` (no inner prefix dir). The 3 empty `<prefix>/` subdirectories are gone.
- `register_template_dir(prefix: str, path: Path)` is the new signature. `init_templates()` builds a `PrefixLoader`-backed `Jinja2Templates`. Lookup strings (`"<prefix>/<name>.html"`) unchanged in all source code.
- Re-registering the same prefix with a different path raises (catches typos at startup).
- All ~558 unit tests + 23 integration tests pass; ruff + basedpyright clean.
