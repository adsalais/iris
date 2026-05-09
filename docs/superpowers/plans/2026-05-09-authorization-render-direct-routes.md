# Authorization render — direct per-intent routes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `/feature/auth/{tab_id}/render` dispatcher with per-intent GET routes that use typed `Session*` deps directly. Delete `intents.py`. Add a `tab_render_url` Jinja global so the shell builds intent-specific URLs.

**Architecture:** One small helper file in shell + one big migration in the auth feature. The shell helper is added first (Task 1) so when the migration lands (Task 2), templates can already reference it.

**Tech Stack:** Python 3.13, FastAPI (typed deps + path/query params), Jinja2 (`env.globals`), pytest. No new runtime deps.

---

## Approach note

This is a "rip and replace" refactor. The old `/render` dispatcher and `intents.py` go away in one commit (Task 2) — splitting that across multiple commits would leave the templates pointing at a URL that doesn't exist yet. Task 1 (the helper) is independent and small enough to land separately.

---

## File map

### New files

| Path | Responsibility |
|---|---|
| `src/iris/shell/url_builders.py` | `tab_render_url(tab) -> str` — single function, ~12 lines including imports |
| `tests/shell/test_tab_render_url.py` | 5 pure unit tests (no params, single param, multiple params, special chars, missing params key) |

### Modified files

| Path | Change |
|---|---|
| `src/iris/app.py` | Two-line addition after `init_templates()` to set `tab_render_url` as a Jinja global |
| `src/iris/features/authorization/routes.py` | Delete `/render` route; add 4 per-intent GET routes with typed deps; rename `submit_create_database` from `POST /submit` to `POST /create_database`; remove `intents` import |
| `src/iris/features/authorization/templates/authorization/create_database.html` | Submit URL changes from `/submit` to `/create_database` |
| `src/iris/shell/templates/shell/_tab_panel.html` | `data-init` URL → `{{ tab_render_url(tab) }}` |
| `src/iris/shell/templates/shell/shell.html` | Same one-line edit on the inline panel block |
| `tests/features/test_authorization_my_access.py` | URLs `/render` → `/my_access` |
| `tests/features/test_authorization_manage.py` | URLs `/render` → `/manage?database=...` |
| `tests/features/test_authorization_audit.py` | URLs `/render` → `/manage?database=...` (audit test exercises the manage render) |
| `tests/features/test_authorization_create_database.py` | Render URLs `/render` → `/create_database`; submit URLs `/submit` → `/create_database` (POST) |
| `tests/features/test_authorization_admin_console.py` | URLs `/render` → `/admin_console` (with optional `?subtab=`) |
| `tests/features/test_authorization_smoke.py` | URL changes for the end-to-end my_access flow |

### Deleted files

| Path | Why |
|---|---|
| `src/iris/features/authorization/intents.py` | Render bodies move into `routes.py` as direct route functions; `RENDER_BY_INTENT` and `IntentHandler` typedef no longer needed |

---

## Task 1 — `tab_render_url` Jinja global + unit test

**Files:**
- Create: `src/iris/shell/url_builders.py`
- Create: `tests/shell/test_tab_render_url.py`
- Modify: `src/iris/app.py` (two-line addition)

- [ ] **Step 1: Write the failing tests**

Create `tests/shell/test_tab_render_url.py`:

```python
"""Unit tests for tab_render_url — pure URL builder, no FastAPI involvement."""
from __future__ import annotations

from iris.shell.url_builders import tab_render_url


def test_no_params():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "my_access", "params": {}}
    assert tab_render_url(tab) == "/feature/auth/ABCD1234/my_access"


def test_single_param():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "manage",
           "params": {"database": "marketing"}}
    assert tab_render_url(tab) == "/feature/auth/ABCD1234/manage?database=marketing"


def test_multiple_params():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "admin_console",
           "params": {"subtab": "users", "extra": "x"}}
    url = tab_render_url(tab)
    assert url.startswith("/feature/auth/ABCD1234/admin_console?")
    assert "subtab=users" in url
    assert "extra=x" in url


def test_special_chars_in_value():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "manage",
           "params": {"database": "needs encoding"}}
    assert tab_render_url(tab) == "/feature/auth/ABCD1234/manage?database=needs%20encoding"


def test_missing_params_key():
    tab = {"feature": "auth", "id": "ABCD1234", "intent": "my_access"}
    assert tab_render_url(tab) == "/feature/auth/ABCD1234/my_access"
```

- [ ] **Step 2: Run to verify tests fail**

```bash
uv run pytest tests/shell/test_tab_render_url.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'iris.shell.url_builders'`.

- [ ] **Step 3: Implement `src/iris/shell/url_builders.py`**

```python
"""URL builders shared between the shell template and feature integrations.

The shell template (and its SSE helpers) emit URLs to render tab panels;
each feature's render routes follow a uniform convention:

    /feature/<feature>/<tab_id>/<intent>?<params encoded as query>

This module exposes ``tab_render_url`` as a Jinja global (registered by
``iris.app.build_app`` after ``init_templates()``) so templates can call
it without each feature needing its own URL builder.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote


def tab_render_url(tab: dict[str, Any]) -> str:
    """Build the GET URL the panel hits to render its content.

    Tab params (the per-tab dict stored in ``session.data``) become the
    URL's query string, supporting auto-injection into typed FastAPI deps
    (e.g. ``database`` → ``SessionDatabaseAdmin``).
    """
    base = f"/feature/{tab['feature']}/{tab['id']}/{tab['intent']}"
    params = tab.get("params") or {}
    if not params:
        return base
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return f"{base}?{qs}"
```

- [ ] **Step 4: Run to verify tests pass**

```bash
uv run pytest tests/shell/test_tab_render_url.py -v
```
Expected: PASS (5 tests).

- [ ] **Step 5: Wire `tab_render_url` as a Jinja global in `src/iris/app.py`**

Find the existing `app.state.templates = init_templates()` line in `build_app()`. Insert two lines immediately after it:

```python
    app.state.templates = init_templates()

    # Register tab_render_url as a Jinja global so shell templates can build
    # per-tab render URLs without each feature owning a URL convention.
    from iris.shell.url_builders import tab_render_url
    app.state.templates.env.globals["tab_render_url"] = tab_render_url
```

- [ ] **Step 6: Run gates**

```bash
uv run ruff check src/iris/shell/url_builders.py src/iris/app.py tests/shell/test_tab_render_url.py
uv run basedpyright --level warning src/iris/shell/url_builders.py src/iris/app.py tests/shell/test_tab_render_url.py
```
Expected: zero issues.

- [ ] **Step 7: Run the full unit suite (regression check — Jinja env modification could in principle affect other tests)**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
```
Expected: 555 (or current baseline) + 5 = 560 tests pass.

- [ ] **Step 8: Commit**

```bash
git add src/iris/shell/url_builders.py src/iris/app.py tests/shell/test_tab_render_url.py
git commit -m "$(cat <<'EOF'
feat(shell): tab_render_url Jinja global

Pure URL builder shared by the shell's _tab_panel and shell.html
templates: /feature/<feature>/<tab_id>/<intent>?<params encoded>.
Registered on app.state.templates.env.globals from build_app() right
after init_templates() (the env doesn't exist before init).

Tab params (dict in session.data) become URL query params, supporting
auto-injection into typed FastAPI deps (e.g. database → SessionDatabaseAdmin).

5 unit tests cover: no params, single param, multiple params,
URL-encoding of special chars, missing params key.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — Per-intent render routes; delete `/render` + `intents.py`; move submit + update templates + tests

Single commit, several coordinated changes. End state: the dispatcher and `intents.py` are gone; each intent has its own GET route at the URL the shell now builds via `tab_render_url`; `POST /submit` becomes `POST /create_database`.

**Files:**
- Modify: `src/iris/features/authorization/routes.py`
- Delete: `src/iris/features/authorization/intents.py`
- Modify: `src/iris/features/authorization/templates/authorization/create_database.html`
- Modify: `src/iris/shell/templates/shell/_tab_panel.html`
- Modify: `src/iris/shell/templates/shell/shell.html`
- Modify: 6 test files (URL strings only)

- [ ] **Step 1: Replace `routes.py` render dispatch with 4 per-intent GET routes**

In `src/iris/features/authorization/routes.py`, find the existing `render` route (the `@router.get("/{tab_id}/render")` block, currently around lines 25–39) and DELETE it. Also find the `submit_create_database` route (currently `@router.post("/{tab_id}/submit")`).

Replace the deleted `/render` block with the 4 per-intent GET routes. Add them at the top of the route definitions (right after `router = APIRouter(prefix="/feature/auth")`):

```python
# ---------------------------------------------------------------------------
# Per-intent render routes — each intent has its own GET with the typed dep
# that gates its capability requirement. The shell's tab_render_url Jinja
# global builds the URL the panel hits.
# ---------------------------------------------------------------------------


@router.get("/{tab_id}/my_access")
async def render_my_access(
    request: Request,
    session: Session,
    tab_id: str,
) -> Response:
    from iris.features.authorization.service import my_access_view
    templates = request.app.state.templates
    ctx = my_access_view(session.capabilities)
    return templates.TemplateResponse(
        request, "authorization/my_access.html",
        {
            "user": session.user,
            "panel_id": tab_panel_id(tab_id),
            **ctx,
        },
    )


@router.get("/{tab_id}/manage")
async def render_manage(
    request: Request,
    db: SessionDatabaseAdmin,
    tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
) -> Response:
    from iris.features.authorization.service import manage_view
    templates = request.app.state.templates
    ctx = await manage_view(db)
    return templates.TemplateResponse(
        request, "authorization/manage.html",
        {
            "panel_id": tab_panel_id(tab_id),
            "tab_id": tab_id,
            "database": db.database,
            **ctx,
        },
    )


@router.get("/{tab_id}/create_database")
async def render_create_database(
    request: Request,
    creator: SessionDatabaseCreator,  # noqa: ARG001  # gates is_admin or can_create_database
    tab_id: str,
) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "authorization/create_database.html",
        {"panel_id": tab_panel_id(tab_id), "tab_id": tab_id, "error": None},
    )


@router.get("/{tab_id}/admin_console")
async def render_admin_console(
    request: Request,
    admin: SessionAdmin,  # noqa: ARG001  # gates is_admin
    tab_id: str,
    subtab: Annotated[str, Query()] = "users",
) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "authorization/admin_console.html",
        {
            "panel_id": tab_panel_id(tab_id),
            "tab_id": tab_id,
            "initial_subtab": subtab,
        },
    )
```

For the two routes that don't reference their typed-session parameter in the body (`render_create_database`, `render_admin_console`), the `# noqa: ARG001` and inline comment document why the typed dep is in the signature even though the body doesn't dereference it. (Per the same convention used by the existing `database: ... # pyright: ignore[reportUnusedParameter]` lines.) ⚠️ Note: ruff reports ARG001 for unused arguments; basedpyright reports `reportUnusedParameter`. If basedpyright complains about these two (it shouldn't — `creator` and `admin` are method names, but verify after the change), add `# pyright: ignore[reportUnusedParameter]` too.

- [ ] **Step 2: Move `POST /submit` → `POST /create_database`**

In `routes.py`, find the existing `@router.post("/{tab_id}/submit")` decorator (the `submit_create_database` function) and change just the path:

```python
@router.post("/{tab_id}/create_database")
async def submit_create_database(
    request: Request,
    creator: SessionDatabaseCreator,
    tab_id: str,
    name: Annotated[str, Query(min_length=0, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    # ... body unchanged
```

The function body stays exactly the same. Only the path string in the decorator changes from `/{tab_id}/submit` to `/{tab_id}/create_database`. (FastAPI handles GET and POST on the same path independently, so this coexists with the `render_create_database` GET added in Step 1.)

- [ ] **Step 3: Remove the `intents` import from `routes.py`**

Find any `from iris.features.authorization.intents import …` line at the top of `routes.py` (or the lazy `from iris.features.authorization.intents import RENDER_BY_INTENT` inside the deleted `/render` route — already gone with the route deletion). Verify the imports block at the top no longer references `intents`. The render route was the only consumer.

- [ ] **Step 4: Delete `src/iris/features/authorization/intents.py`**

```bash
rm src/iris/features/authorization/intents.py
```

Verify no other file imports it:

```bash
grep -rn "from iris.features.authorization.intents\|iris.features.authorization import intents" src/ tests/
```
Expected: zero matches.

- [ ] **Step 5: Update `create_database.html` submit URL**

In `src/iris/features/authorization/templates/authorization/create_database.html`, change the form's `data-on:submit` URL. Replace `/submit?name=` with `/create_database?name=`:

```html
<div class="iris-feature-page" id="{{ panel_id }}">
  <h2>Create database</h2>
  <form id="{{ panel_id }}-form"
        data-on:submit="@post('/feature/auth/{{ tab_id }}/create_database?name=' + encodeURIComponent($tabs.{{ tab_id }}.new_db_name || ''))">
    <label>
      Database name
      <input data-bind="tabs.{{ tab_id }}.new_db_name"
             placeholder="e.g. marketing">
    </label>
    {% if error %}
    <p class="iris-error" id="{{ panel_id }}-error">{{ error }}</p>
    {% endif %}
    <button type="submit">Create</button>
  </form>
</div>
```

- [ ] **Step 6: Update shell templates to use `tab_render_url`**

In `src/iris/shell/templates/shell/_tab_panel.html`, replace the file's contents with:

```html
<div id="tab-content-{{ tab.id }}"
     class="iris-tab-panel"
     data-show="$active === {{ tab.id | tojson }}"
     data-init="@get('{{ tab_render_url(tab) }}')">
</div>
```

In `src/iris/shell/templates/shell/shell.html`, find the inline panel-rendering loop (the part that emits `<div id="tab-content-{{ tab.id }}" …>` inside `<div id="tab-content" class="iris-tab-content">`). Change the `data-init` line the same way:

```html
      <div id="tab-content-{{ tab.id }}"
           class="iris-tab-panel"
           data-show="$active === {{ tab.id | tojson }}"
           data-init="@get('{{ tab_render_url(tab) }}')">
      </div>
```

- [ ] **Step 7: Update test URLs — 6 files**

Each test file uses different intents/URLs. Update mechanically.

**`tests/features/test_authorization_my_access.py`**: every `client.get("/feature/auth/<TAB_ID>/render")` becomes `client.get("/feature/auth/<TAB_ID>/my_access")`. The fixture seed plants `intent: "my_access"` so this is consistent.

```bash
sed -i 's|/feature/auth/AB12CD34/render|/feature/auth/AB12CD34/my_access|g' tests/features/test_authorization_my_access.py
```

Edge case in `test_my_access_render_route_returns_404_for_wrong_feature` and `test_my_access_render_route_returns_404_for_unknown_intent` — these test the OLD dispatcher's 404 paths. They no longer apply (no central dispatcher). Either:
- Delete both tests (the per-intent routes don't have an "unknown intent" case; FastAPI returns 404 for an unknown URL natively, which is implicitly tested by hitting any nonexistent path)
- Or keep one as a sanity check that hitting a nonexistent intent path returns 404

I recommend deletion; FastAPI's URL routing is well-tested upstream.

**`tests/features/test_authorization_manage.py`**: every `client.get("/feature/auth/<TAB_ID>/render")` becomes `client.get("/feature/auth/<TAB_ID>/manage?database=marketing")` (or whatever the seed's database is — verify with `grep`). The seed in this file uses `database = "marketing"`.

```bash
sed -i 's|/feature/auth/MG12CD34/render|/feature/auth/MG12CD34/manage?database=marketing|g' tests/features/test_authorization_manage.py
```

**`tests/features/test_authorization_audit.py`**: this test exercises the manage render path (the audit section of the manage page). Same change.

```bash
sed -i 's|/feature/auth/AU12CD34/render|/feature/auth/AU12CD34/manage?database=marketing|g' tests/features/test_authorization_audit.py
```

**`tests/features/test_authorization_create_database.py`**: render URL `/feature/auth/<TAB_ID>/render` → `/feature/auth/<TAB_ID>/create_database`; submit URL `/feature/auth/<TAB_ID>/submit?name=...` → `/feature/auth/<TAB_ID>/create_database?name=...`. Same TAB_ID prefix.

```bash
sed -i 's|/feature/auth/CR12CD34/render|/feature/auth/CR12CD34/create_database|g; s|/feature/auth/CR12CD34/submit|/feature/auth/CR12CD34/create_database|g' tests/features/test_authorization_create_database.py
```

**`tests/features/test_authorization_admin_console.py`**: every `/render` becomes `/admin_console`. The tests with no subtab in seed should hit `/admin_console` (route default `subtab="users"` applies); tests that explicitly set the subtab in the seed should pass `?subtab=...` in the URL too (verify by reading the file). Most tests in this file leave subtab default; the URL is just `/admin_console`.

```bash
sed -i 's|/feature/auth/AC12CD34/render|/feature/auth/AC12CD34/admin_console|g' tests/features/test_authorization_admin_console.py
```

**`tests/features/test_authorization_smoke.py`**: change `/feature/auth/<tab_id>/render` to `/feature/auth/<tab_id>/my_access`. The smoke test exercises my_access end-to-end. The `tab_id` is a runtime value (extracted from the SSE), so use a Python f-string or string concat in the test code itself, NOT sed:

Open `tests/features/test_authorization_smoke.py` and find the line:

```python
render_r = authed_client.get(f"/feature/auth/{tab_id}/render")
```

Change to:

```python
render_r = authed_client.get(f"/feature/auth/{tab_id}/my_access")
```

- [ ] **Step 8: Run the affected test files individually first to catch obvious URL typos**

```bash
uv run pytest tests/features/test_authorization_my_access.py tests/features/test_authorization_manage.py tests/features/test_authorization_audit.py tests/features/test_authorization_create_database.py tests/features/test_authorization_admin_console.py tests/features/test_authorization_smoke.py -v
```
Expected: All pass. If a URL is wrong, the test returns 404 instead of 200/403 and fails fast.

- [ ] **Step 9: Run the full unit suite + gates**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero failures, zero issues. Test count drops by 2 if the two `_returns_404_*` tests were deleted from `test_authorization_my_access.py` (Step 7); otherwise unchanged.

- [ ] **Step 10: Optional — integration regression**

```bash
uv run pytest tests/clickhouse/integration tests/auth/integration -q
```
Expected: 23 integration tests pass (none touch the auth feature's render path; this is a sanity check).

- [ ] **Step 11: Commit**

```bash
git add src/iris/features/authorization/routes.py \
        src/iris/features/authorization/templates/authorization/create_database.html \
        src/iris/shell/templates/shell/_tab_panel.html \
        src/iris/shell/templates/shell/shell.html \
        tests/features/test_authorization_my_access.py \
        tests/features/test_authorization_manage.py \
        tests/features/test_authorization_audit.py \
        tests/features/test_authorization_create_database.py \
        tests/features/test_authorization_admin_console.py \
        tests/features/test_authorization_smoke.py
git rm src/iris/features/authorization/intents.py
git commit -m "$(cat <<'EOF'
refactor(features/authorization): direct per-intent render routes

Replace the GET /feature/auth/{tab_id}/render dispatcher with 4
per-intent routes that use typed Session* deps directly:

  GET  /feature/auth/{tab_id}/my_access            → Session
  GET  /feature/auth/{tab_id}/manage?database=...  → SessionDatabaseAdmin
  GET  /feature/auth/{tab_id}/create_database      → SessionDatabaseCreator
  GET  /feature/auth/{tab_id}/admin_console        → SessionAdmin
       [?subtab=users|databases|policies|audit]

Each route's authz requirement is its function signature. The 4 inline
capability checks that intents.py had are gone — the typed deps
enforce them. RENDER_BY_INTENT and the IntentHandler typedef are
deleted with intents.py.

Bonus consolidation: POST /{tab_id}/submit becomes
POST /{tab_id}/create_database — same path as the render GET, RESTful.

The shell template's data-init URL switches from a hardcoded /render
to {{ tab_render_url(tab) }}, which builds the per-intent URL via the
Jinja global registered in build_app() (added in the previous commit).

URLs visible at the address bar now name the intent explicitly. Tests
update mechanically: existing /render hits become /<intent>?<params>
hits, and the create_database submit hit changes from /submit to
/create_database. Status codes preserved across all paths.

The intent dispatcher (IntentDispatcher.check at POST /api/tabs) is
unchanged — defense-in-depth layer 2 still gates tab opening.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Recap

2 tasks, 2 commits. End state:

- `intents.py` and the `/render` dispatcher are gone.
- Each intent has its own GET route with typed dep at `/feature/auth/{tab_id}/{intent}`.
- `POST /create_database` replaces `POST /submit`.
- `tab_render_url` Jinja global builds the convention `/feature/<feature>/<tab_id>/<intent>?<params>` for ALL features.
- Shell templates use the helper; no more hardcoded `/render`.
- All 555 unit tests + 23 integration tests pass; ruff + basedpyright clean.
