# Authorization render — direct per-intent routes

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

`src/iris/features/authorization/intents.py` exists today only as a string-keyed dispatcher: each tab opens a panel that hits `GET /feature/auth/{tab_id}/render`, the route looks up `tab.intent`, and `RENDER_BY_INTENT` maps the intent name to the right render function. Three of the four handlers in that file still do inline capability promotion (the same smell we deleted from `routes.py` in the typed-deps refactor).

The dispatcher has no real value. FastAPI already maps URLs to handlers natively — emitting per-intent URLs from the shell at tab-open time gives every render its own typed FastAPI route, with the right `Session*` dep auto-resolved. `intents.py` and `RENDER_BY_INTENT` go away entirely.

## Goal

Each authorization-feature intent renders via its own GET route with the right typed `Session*` dep declared in the function signature. The shell builds intent-specific URLs from a generic Jinja helper (`tab_render_url(tab)`); features stay free of any per-feature URL convention. `intents.py` is deleted.

## Non-goals

- **Intent gate stays.** `IntentDispatcher.check` is still called at `POST /api/tabs` to gate tab opening (defense-in-depth layer 2). This refactor only removes the *render*-time dispatch.
- **Other features (workbench / dashboards / ingestion).** They will adopt the same `tab_render_url` convention when they add render routes; this spec lands the helper but doesn't touch features that don't yet exist.
- **URL hierarchy redesign for action routes.** Members / row-policies / admin sub-tabs / delete-database / reprovision keep the URLs they got in the typed-deps refactor.
- **Service-layer changes.** `service.py` (`my_access_view`, `manage_view`, `list_all_*`) is unchanged.

## 1. The four render routes

Each intent gets a GET route in `routes.py`. The capability requirement is the typed dep.

| Intent | URL | Dep | Body summary |
|---|---|---|---|
| my_access | `GET /feature/auth/{tab_id}/my_access` | `Session` | render `my_access_view(session.capabilities)` |
| manage | `GET /feature/auth/{tab_id}/manage?database=<db>` | `SessionDatabaseAdmin` | render `await manage_view(session)` |
| create_database | `GET /feature/auth/{tab_id}/create_database` | `SessionDatabaseCreator` | render the form |
| admin_console | `GET /feature/auth/{tab_id}/admin_console?subtab=<users\|databases\|policies\|audit>` | `SessionAdmin` | render the panel + initial subtab |

`tab_id` is in every URL because each render emits HTML with `id="t-<tab_id>-..."` selectors and the panel is `id="tab-content-<tab_id>"`. The render route uses `tab_id` to namespace the rendered fragment, NOT to look up the tab — the typed dep already gates by capability without needing the tab record.

For routes that don't need a query parameter (my_access, create_database), no `?...`.

## 2. Bonus consolidation: submit moves onto the create_database path

Today: `POST /feature/auth/{tab_id}/submit` is the create-database submit handler.

After: `POST /feature/auth/{tab_id}/create_database` is the submit. GET on the same path renders the form. RESTful pairing; one fewer URL to remember.

The form template (`create_database.html`) updates its `data-on:submit` URL accordingly.

## 3. Shell-side URL builder — `tab_render_url(tab)`

The shell template currently emits a hardcoded `/feature/<feature>/<tab_id>/render` for every panel. With per-intent URLs, the shell needs to build a per-tab URL.

Add a single Jinja global, registered in `iris.shell.install` after `init_templates()`:

```python
# src/iris/shell/url_builders.py
from urllib.parse import quote


def tab_render_url(tab: dict) -> str:
    """Build the GET URL the panel hits to render its content.

    Convention used by all features:

        /feature/<feature>/<tab_id>/<intent>?<params encoded as query>

    Tab params (the per-tab dict stored in session.data) become the URL's
    query string, supporting auto-injection into typed FastAPI deps
    (e.g. ``database`` → SessionDatabaseAdmin).
    """
    base = f"/feature/{tab['feature']}/{tab['id']}/{tab['intent']}"
    params = tab.get("params") or {}
    if not params:
        return base
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    return f"{base}?{qs}"
```

Registration in `iris.shell.install`:

```python
def install(app: FastAPI) -> None:
    app.state.contributions = Contributions()
    app.state.intent_dispatcher = IntentDispatcher()
    register_template_dir(Path(__file__).parent / "templates")
    app.mount(
        "/static/shell",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="shell-static",
    )
    from iris.shell.routes import install_routes
    install_routes(app)
```

The template registration happens in `build_app` AFTER `init_templates()` (which is when the env exists). Add to `iris.app.build_app` immediately after the templates-init line:

```python
app.state.templates = init_templates()
from iris.shell.url_builders import tab_render_url
app.state.templates.env.globals["tab_render_url"] = tab_render_url
```

(The Jinja env exposed by `Jinja2Templates` is the `.env` attribute; globals are mutable.)

## 4. Template edits

### `src/iris/shell/templates/shell/_tab_panel.html` (the SSE-on-tab-open partial)

Today:

```html
<div id="tab-content-{{ tab.id }}"
     class="iris-tab-panel"
     data-show="$active === {{ tab.id | tojson }}"
     data-init="@get('/feature/{{ tab.feature }}/{{ tab.id }}/render')">
</div>
```

After:

```html
<div id="tab-content-{{ tab.id }}"
     class="iris-tab-panel"
     data-show="$active === {{ tab.id | tojson }}"
     data-init="@get('{{ tab_render_url(tab) }}')">
</div>
```

### `src/iris/shell/templates/shell/shell.html` (the initial-load page seed)

The same single-line change to the panel block in the per-tab loop.

### `src/iris/features/authorization/templates/authorization/create_database.html`

The `data-on:submit` URL changes from `/submit` to `/create_database` to match the new submit endpoint:

```html
<form id="{{ panel_id }}-form"
      data-on:submit="@post('/feature/auth/{{ tab_id }}/create_database?name=' + encodeURIComponent($tabs.{{ tab_id }}.new_db_name || ''))">
```

## 5. `intents.py` deleted; bodies move to `routes.py`

Each render becomes a route function in `routes.py` whose body inlines what the corresponding `intents.render_*` function used to do. Bodies are short — they call the relevant `service.*_view` (or read `session.capabilities` directly for `my_access`) and return a `templates.TemplateResponse`.

Sample for `manage` (the most involved):

```python
@router.get("/{tab_id}/manage")
async def render_manage(
    request: Request,
    db: SessionDatabaseAdmin,
    tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],  # consumed by SessionDatabaseAdmin dep  # pyright: ignore[reportUnusedParameter]
) -> Response:
    from iris.features.authorization.service import manage_view
    ctx = await manage_view(db)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "authorization/manage.html",
        {
            "panel_id": tab_panel_id(tab_id),
            "tab_id": tab_id,
            "database": db.database,
            **ctx,
        },
    )
```

`my_access`, `create_database`, and `admin_console` follow the same shape — typed dep + small body.

The `IntentHandler` typedef and `RENDER_BY_INTENT` dict are deleted with `intents.py`.

## 6. The old `/render` route is deleted

Remove `@router.get("/{tab_id}/render")` from `routes.py` entirely. The shell never emits that URL after the template change.

## 7. Files

| Path | Change |
|---|---|
| `src/iris/shell/url_builders.py` | **New**, ~15 lines: `tab_render_url(tab)` helper |
| `src/iris/shell/install.py` | (No change — registration happens in `iris.app.build_app` since the env doesn't exist until after `init_templates()`) |
| `src/iris/app.py` | Two-line addition after `init_templates()` to set `tab_render_url` as a Jinja global |
| `src/iris/shell/templates/shell/_tab_panel.html` | `data-init` URL → `tab_render_url(tab)` |
| `src/iris/shell/templates/shell/shell.html` | Same one-line edit on the inline panel block |
| `src/iris/features/authorization/routes.py` | Delete `/render`; add 4 per-intent GET routes; rename submit POST `/submit` → POST `/create_database`; remove the `intents.py` import |
| `src/iris/features/authorization/intents.py` | **Delete** |
| `src/iris/features/authorization/templates/authorization/create_database.html` | `data-on:submit` URL → `/create_database` |
| `tests/features/test_authorization_my_access.py` | URLs `/render` → `/my_access` |
| `tests/features/test_authorization_manage.py` | URLs `/render` → `/manage?database=...` |
| `tests/features/test_authorization_create_database.py` | URLs `/render` → `/create_database`; submit URL `/submit` → `/create_database` (POST) |
| `tests/features/test_authorization_admin_console.py` | URLs `/render` → `/admin_console?subtab=...` |
| `tests/features/test_authorization_audit.py` | URLs `/render` → `/manage?database=...` (this test exercises the manage render path) |
| `tests/features/test_authorization_smoke.py` | URL changes for the end-to-end my_access flow |
| `tests/shell/test_tab_render_url.py` | **New**: pure unit test of `tab_render_url` (4 shapes: no params, one param, multiple params, special chars) |

Total: 1 file deleted, 1 file added (helper), 1 file added (test), and one-line-or-few-line edits across the rest.

## 8. Tests

### Pure unit test for `tab_render_url`

```python
# tests/shell/test_tab_render_url.py
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

### Existing test URL updates

Mechanical: every test that hits `/feature/auth/<tab_id>/render` updates to the per-intent URL. The seed helpers in those test files already set the right intent on the tab; we just change the URL we GET.

For `test_authorization_create_database.py`, the submit POST also changes (`/submit` → `/create_database`). One additional URL update per test that exercises submit.

### Status code semantics preserved

The previous `/render` endpoint returned 404 for unknown tab + 404 for wrong feature. The new per-intent routes:
- For my_access (Session, any signed-in): 200 if session is valid; 401 if not (handled by Session dep).
- For manage / create_database / admin_console: 403 if cap fails (handled by typed dep); 200 otherwise.

Important difference: the old route returned 404 for "tab not found in session.data". The new routes don't look up the tab — they trust the URL. If a stale tab_id reaches the server, the route succeeds and returns HTML; the client's panel just doesn't visibly exist (already removed). Acceptable per the existing "trust the URL" convention from the typed-deps refactor.

For the `manage` route specifically, the URL says `?database=marketing` and the dep gates on admin of `marketing`. The user has admin → 200. The user does not → 403. Tab existence is irrelevant to the security decision.

## 9. Risks and tradeoffs

- **`tab_render_url` becomes a shared convention.** All features must follow `/feature/<feature>/<tab_id>/<intent>?<params>`. If a future feature wants a different render URL shape (e.g., extra path segments), they'd need to either fit the convention or extend the helper. YAGNI today — defer until a real exception appears.
- **Intent name appears in URL.** `manage`, `my_access`, `admin_console` are visible in the address bar (when devtools is open). Security-neutral; mildly informative ("ah, that's the admin console URL").
- **Trust the URL — no tab lookup at render.** The old route confirmed tab existence in `session.data['tabs']`. The new routes don't. If a client builds a URL with a stale `tab_id`, the render succeeds (the URL says what it says, the dep gates correctly). Same tradeoff as the typed-deps refactor.
- **The `database` query param on `manage` is unused inside the route body** (the dep consumes it). Same `# pyright: ignore[reportUnusedParameter]` pattern as the existing manage routes — established precedent.
- **`intents.py` deletion drops `IntentHandler` typedef.** No other module imports it. Confirmed via grep before merge.
