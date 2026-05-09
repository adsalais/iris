# Shell static + template-namespace flattening

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

Two unrelated-but-complementary cleanups, bundled because both touch shell wiring and benefit from one merge cycle:

1. **`datastar.js` lives at `src/iris/static/datastar.js`** with a dedicated `app.mount("/static", …)` in `iris.app.build_app`. The file is loaded only by `shell.html` — no other subsystem references it. The location and mount predate the shell module; today they're a holdover, not a feature boundary.

2. **Templates use a `<module>/templates/<module>/<name>.html` double-nested directory layout** (e.g. `auth/templates/auth/forbidden.html`). The inner directory exists as a *namespace* for Jinja's lookup string (`"auth/forbidden.html"`); today the namespace IS the directory. The convention works but the directory layout has one redundant level. Jinja's `PrefixLoader` lets us declare the prefix at registration time instead of through the directory layout — same lookup strings, one less level of nesting.

## Goal

Clean up the two warts above with minimal blast radius. Lookup strings in code (`templates.TemplateResponse(request, "auth/forbidden.html", …)`) stay unchanged; only the underlying file paths and the shell static mount are touched.

## Non-goals

- **`core/` (or `infra/`) parent dir for `auth` + `clickhouse`** — explicitly deferred. Today there are only 2 candidates for that bucket; the grouping isn't load-bearing yet. Revisit when a third candidate appears.
- **Per-subsystem Jinja loaders** — the existing single global loader stays, just reshaped via `PrefixLoader` so prefixes are explicit registration parameters.
- **`/static/datastar.js` URL backwards-compatibility** — the URL changes to `/static/shell/datastar.js`; no redirect or alias. The only consumer is the shell template, which we update in the same commit.
- **Renaming any registered prefix** — `auth`, `shell`, `authorization` keep their current names (they're already in the lookup strings; renaming would churn every TemplateResponse call site).

## 1. `datastar.js` → `shell/static/`

### File move

| From | To |
|---|---|
| `src/iris/static/datastar.js` | `src/iris/shell/static/datastar.js` |

### Mount removal

In `iris.app.build_app`, delete the global `/static` mount:

```python
# DELETE:
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)
```

The shell already mounts `/static/shell/` from `src/iris/shell/static/` in `iris.shell.install`, so the moved file becomes available at `/static/shell/datastar.js` automatically.

### Template URL update

`src/iris/shell/templates/shell.html` currently has:

```html
<script type="module" src="/static/datastar.js"></script>
```

After the move, `shell.html` is at `src/iris/shell/templates/shell.html` (per §2 below). Update the script tag to:

```html
<script type="module" src="/static/shell/datastar.js"></script>
```

### Test updates

`tests/test_static_assets.py` currently has two tests:

```python
def test_static_datastar_js_is_served():
    # GET /static/datastar.js → 200, application/javascript

def test_static_mount_404s_for_missing_file():
    # GET /static/does-not-exist.js → 404
```

Update the URLs:

```python
def test_static_datastar_js_is_served():
    # GET /static/shell/datastar.js → 200, application/javascript

def test_static_mount_404s_for_missing_file():
    # GET /static/shell/does-not-exist.js → 404
```

(The second test still meaningfully asserts the shell mount doesn't serve arbitrary names. Status code expectations unchanged.)

### Cleanup

Delete the now-empty `src/iris/static/` directory.

## 2. Template directory flattening via `PrefixLoader`

### Files moved (no content change)

For each module, move every `templates/<prefix>/*.html` up one level to `templates/*.html`:

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

Then `git rm -r` the now-empty `<module>/templates/<prefix>/` directories (3 directories total).

### Inter-template `{% include "<prefix>/<name>.html" %}` references

Templates that include other templates use the prefixed name. These references stay the same (e.g. `{% include "shell/_top_buttons.html" %}` continues to work because the PrefixLoader resolves `"shell/..."` to `shell/templates/...`). No edits needed.

### `register_template_dir` signature change

In `src/iris/templates.py`:

```python
# Before
_dirs: list[Path] = []

def register_template_dir(path: Path) -> None: ...

def init_templates() -> Jinja2Templates:
    return Jinja2Templates(directory=_dirs)
```

After:

```python
from jinja2 import ChoiceLoader, FileSystemLoader, PrefixLoader

_prefixed_dirs: dict[str, Path] = {}


def register_template_dir(prefix: str, path: Path) -> None:
    """Register `path` to serve templates under the `<prefix>/...` namespace.

    Idempotent: re-registering the same (prefix, path) pair is a no-op.
    Re-registering the same prefix with a DIFFERENT path raises (catches
    a typo at startup; if you need to alias a prefix, it's a separate
    helper).
    """
    resolved = Path(path)
    existing = _prefixed_dirs.get(prefix)
    if existing == resolved:
        return  # idempotent
    if existing is not None:
        msg = (
            f"prefix {prefix!r} already registered with path "
            f"{existing!r}; cannot re-register with {resolved!r}"
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
    loader = PrefixLoader({
        prefix: FileSystemLoader(path)
        for prefix, path in _prefixed_dirs.items()
    })
    # Jinja2Templates accepts `directory=` for the simple case. For the
    # PrefixLoader case, build env directly via the env constructor; pass
    # to Jinja2Templates via a thin shim that wraps the env.
    templates = Jinja2Templates(env=Environment(loader=loader, autoescape=select_autoescape()))
    return templates
```

(Final implementation may need a small tweak — `Jinja2Templates` constructor accepts either `directory=` or `env=`, plus `autoescape=` defaults need to match Starlette's defaults. The plan will pin down the exact construction.)

### Registration call sites updated

| Caller | Before | After |
|---|---|---|
| `iris/auth/routes.py` `install` | `register_template_dir(Path(__file__).parent / "templates")` | `register_template_dir("auth", Path(__file__).parent / "templates")` |
| `iris/shell/install.py` `install` | `register_template_dir(Path(__file__).parent / "templates")` | `register_template_dir("shell", Path(__file__).parent / "templates")` |
| `iris/features/authorization/install.py` `install` | `register_template_dir(Path(__file__).parent / "templates")` | `register_template_dir("authorization", Path(__file__).parent / "templates")` |
| `tests/auth/test_exception_handler.py` `_build_app` | `register_template_dir(Path(iris.__file__).parent / "auth" / "templates")` | `register_template_dir("auth", Path(iris.__file__).parent / "auth" / "templates")` |
| `tests/auth/test_provider_oauth.py` `_build_app` | same | `register_template_dir("auth", Path(iris.__file__).parent / "auth" / "templates")` |
| `tests/auth/test_provider_mock.py` (5 sites) | same | `register_template_dir("auth", Path(iris.__file__).parent / "auth" / "templates")` |
| `tests/shell/test_templates_loader.py` (4 tests) | `register_template_dir(d1)` | `register_template_dir("a", d1)` (and similar) — tests target `templates.get_template("a/<name>.html")` |

### Lookup strings unchanged

Code that calls `templates.TemplateResponse(request, "<prefix>/<name>.html", …)` is untouched. The prefix is still in the lookup string (matches the registered prefix); the file is now one level shallower in the filesystem; PrefixLoader bridges the two.

## 3. Files

| Path | Change |
|---|---|
| `src/iris/static/datastar.js` | **Move** to `src/iris/shell/static/datastar.js` |
| `src/iris/static/` (directory) | **Delete** (now empty) |
| `src/iris/app.py` | Delete the global `/static` mount; the `Path` import survives if used elsewhere |
| `src/iris/templates.py` | `register_template_dir` gains a `prefix` parameter; `init_templates` builds a `PrefixLoader`-backed `Jinja2Templates` |
| `src/iris/auth/routes.py` | Pass `"auth"` to `register_template_dir` |
| `src/iris/shell/install.py` | Pass `"shell"` to `register_template_dir` |
| `src/iris/features/authorization/install.py` | Pass `"authorization"` to `register_template_dir` |
| `src/iris/auth/templates/auth/*.html` (2 files) | **Move** up one level (drop `auth/`) |
| `src/iris/auth/templates/auth/` (directory) | **Delete** (now empty) |
| `src/iris/shell/templates/shell/*.html` (5 files) | **Move** up one level (drop `shell/`); also edit `shell.html` to update the datastar script src to `/static/shell/datastar.js` |
| `src/iris/shell/templates/shell/` (directory) | **Delete** (now empty) |
| `src/iris/features/authorization/templates/authorization/*.html` (12 files) | **Move** up one level (drop `authorization/`) |
| `src/iris/features/authorization/templates/authorization/` (directory) | **Delete** (now empty) |
| `tests/test_static_assets.py` | URLs `/static/datastar.js` → `/static/shell/datastar.js` and `/static/does-not-exist.js` → `/static/shell/does-not-exist.js` |
| `tests/auth/test_exception_handler.py` | `register_template_dir` call gains `"auth"` prefix |
| `tests/auth/test_provider_mock.py` | Same, in 5 places |
| `tests/auth/test_provider_oauth.py` | Same |
| `tests/shell/test_templates_loader.py` | Update tests for new signature; tests should target `templates.get_template("<prefix>/<name>")` after registration |

Total: 1 directory deletion (3 nested), 19+ file moves (datastar + 2 + 5 + 12 templates), ~10 source edits, ~12 test edits.

## 4. Tests

### Unchanged (validate-the-refactor coverage)

The existing 558-test suite has end-to-end coverage of every render path through `templates.TemplateResponse(request, "<prefix>/<name>.html", …)`. If the lookup paths break, the tests break loudly. No new tests required for the move (the existing suite IS the regression check).

### Updated

- `tests/test_static_assets.py` — URLs change as above (~2 lines).
- `tests/shell/test_templates_loader.py` — every `register_template_dir(path)` becomes `register_template_dir("<prefix>", path)`. The `test_first_match_wins_when_paths_collide` test specifically — under PrefixLoader, two `register_template_dir("a", path)` calls with the same prefix raise instead of silently picking the first. That test's behavior is the test for the OLD `FileSystemLoader(directory=[...])` semantics; under PrefixLoader, "first registered wins on collision" no longer applies the same way — instead, "duplicate prefix registration raises" is the new contract. Rewrite that test as `test_duplicate_prefix_registration_raises`.

### New

- `tests/shell/test_templates_loader.py::test_register_idempotent_with_same_path` — calling `register_template_dir("a", d1)` twice with the same `d1` is a no-op (no raise).
- `tests/shell/test_templates_loader.py::test_register_different_path_for_same_prefix_raises` — calling `register_template_dir("a", d1)` then `register_template_dir("a", d2)` raises `ValueError`.

## 5. Risks and tradeoffs

- **`PrefixLoader` is one extra layer of Jinja indirection.** Performance-neutral in practice (the loader resolution is in-memory and Jinja caches compiled templates). Negligible.
- **Re-registering the same prefix with a different path now raises** instead of silently picking the first. This is a behavior change from the old "first registered wins" semantics, but it catches typos at startup. If a real use case wants to alias a prefix to multiple paths (it doesn't today), we'd add `register_template_alias("auth", path)` later.
- **`Jinja2Templates` constructor with a custom env.** Starlette's `Jinja2Templates(env=...)` is supported but less common than `directory=...`. The plan needs to verify the exact construction works (autoescape config, request global). Starlette ≥ 0.30 supports this directly.
- **Moving `datastar.js` to shell/static breaks the `/static/datastar.js` URL.** Out-of-scope by spec — there is no other consumer. If a future feature needs to reference Datastar from a non-shell page, it'd reach into `/static/shell/datastar.js` (cross-feature URL; same trade-off as a `/static/auth/...` would be). No real concern at present.
- **The `core/` parent-dir refactor stays explicitly deferred.** When a third candidate emerges (likely an audit-log or metrics module), revisit then with concrete examples of what goes in the bucket.
