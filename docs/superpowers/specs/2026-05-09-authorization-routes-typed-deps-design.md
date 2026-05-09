# Authorization routes — typed deps refactor

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

`src/iris/features/authorization/routes.py` (483 lines, 22 routes) currently uses the bare `session: Session` annotation on every route and does manual capability promotion in the route body via three helpers: `_promote_to_admin`, `_promote_to_db_admin`, `_members_route_common`. The capability requirement of each route is buried inside those helper calls. Reading the file, you cannot tell what authz a route needs without tracing into helper bodies.

Iris already has the typed `Session*` deps that solve this problem (`SessionAdmin`, `SessionDatabaseCreator`, `SessionDatabaseAdmin`) — they both promote and gate. The authorization feature didn't use them because `SessionDatabaseAdmin` requires `database: str` to be a path/query parameter (FastAPI auto-injects), and the feature's URLs only carry `tab_id`. The database lives in `session.data['tabs'][i].params['database']`.

This spec adds `database` as a query parameter to the routes that need it, so the existing typed deps slot in directly. No new deps, no new abstractions.

## Goal

Each action route's signature declares its authz requirement via the existing typed `Session*` annotation. Three promotion helpers and the `_members_route_common` lookup are deleted. Routes shrink to one or two lines of body.

## Non-goals

- **`intents.py` refactor.** The render-route dispatch (`RENDER_BY_INTENT`) and per-intent promotion in `intents.py` stay as-is. A separate spec will tackle that later — see the deferred-Option-B sketch in the brainstorm transcript.
- **URL hierarchy redesign.** Existing path shape `/feature/auth/{tab_id}/...` is preserved; only the query string grows by one parameter on the affected routes.
- **New dep classes.** No `SessionTabDatabaseAdmin` or similar. Use what's already in `iris.auth.deps`.
- **Cross-checking `tab_id` against `database`.** If the URL says `tab_id=X&database=Y` and the open tab `X` actually manages a different database, the route honors `database=Y` (the user has admin on `Y`, by the dep gate; that's all the authorization that matters). The tab is only used as a rendering anchor.

## 1. Routes summary

22 routes total in routes.py. Three flavors after the refactor:

| Group | Count | New signature shape | Existing dep |
|---|---|---|---|
| Members `POST/DELETE /{tab_id}/members/{tier}/{user|group}` | 12 | `db: SessionDatabaseAdmin, tab_id: str, database: str (Query), …` | `SessionDatabaseAdmin` |
| Row policies `POST/DELETE /{tab_id}/policies` | 2 | `db: SessionDatabaseAdmin, tab_id: str, database: str (Query), …` | `SessionDatabaseAdmin` |
| Delete database `DELETE /{tab_id}/database` | 1 | `db: SessionDatabaseAdmin, tab_id: str, database: str (Query), confirm: str` | `SessionDatabaseAdmin` |
| Admin console GETs `GET /{tab_id}/admin/{users,databases,policies,audit}` | 4 | `admin: SessionAdmin, tab_id: str` | `SessionAdmin` |
| Reprovision `POST /{tab_id}/admin/users/{username}/reprovision` | 1 | `admin: SessionAdmin, tab_id: str, username: str` | `SessionAdmin` |
| Create database submit `POST /{tab_id}/submit` | 1 | `creator: SessionDatabaseCreator, tab_id: str, name: str` | `SessionDatabaseCreator` |
| Render `GET /{tab_id}/render` | 1 | unchanged: `session: Session, tab_id: str` | dispatch-by-intent stays |

Total typed: 21 routes. The render route keeps `Session` because dispatch is dynamic (see Non-goals).

## 2. URL shape

Add `database` to the query string of the 15 `SessionDatabaseAdmin`-using routes. Path is unchanged.

Examples (deltas in **bold**):

```
POST   /feature/auth/{tab_id}/members/reader/user?**database=marketing**&username=bob
DELETE /feature/auth/{tab_id}/members/admin/group?**database=marketing**&group=data-team
POST   /feature/auth/{tab_id}/policies?**database=marketing**&table=events&column=user_id&role=R&value=alice
DELETE /feature/auth/{tab_id}/policies?**database=marketing**&table=events&role=R&value=alice
DELETE /feature/auth/{tab_id}/database?**database=marketing**&confirm=marketing
```

Admin console, reprovision, submit (create_database), and render — URLs unchanged.

## 3. Routes.py after refactor

Sample for one route per flavor; the rest follow the same pattern.

### Members route (1 of 12)

```python
@router.post("/{tab_id}/members/reader/user")
async def grant_reader_user(
    request: Request,
    db: SessionDatabaseAdmin,
    tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.grant_reader(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)
```

`database` is in the signature (gated by `SessionDatabaseAdmin`); the body is two lines.

### Admin console route (1 of 5)

```python
@router.get("/{tab_id}/admin/users")
async def admin_users(
    request: Request,
    admin: SessionAdmin,
    tab_id: str,
) -> Response:
    users = await list_all_users(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_users.html").render(
        panel_id=panel_id, tab_id=tab_id, users=users,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))
```

`SessionAdmin` is the visible authz; route body is purely the per-route work.

### Submit create_database

```python
@router.post("/{tab_id}/submit")
async def submit_create_database(
    request: Request,
    creator: SessionDatabaseCreator,
    tab_id: str,
    name: Annotated[str, Query(min_length=0, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    from iris.shell.tabs import TabRecord, find_tab, replace_tab

    rec = find_tab(creator.data, tab_id)  # creator inherits AuthSession.data
    if rec is None or rec.feature != "auth" or rec.intent != "create_database":
        raise HTTPException(status_code=404, detail="tab not found")
    # ... rest unchanged: try create_database, on success replace_tab + SSE,
    # on error re-render form with inline error.
```

The `is_admin or can_create_database` check is gone — `SessionDatabaseCreator` enforces it via the `_require_database_creator` dep.

### Delete database

```python
@router.delete("/{tab_id}/database")
async def delete_database(
    db: SessionDatabaseAdmin,
    tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    confirm: Annotated[str, Query(min_length=0, max_length=255)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    from iris.shell.tabs import remove_tab

    if confirm != database:
        raise HTTPException(
            status_code=400,
            detail="confirmation does not match the database name",
        )
    await db.delete_database()
    remove_tab(db.data, tab_id)  # no-op if tab_id doesn't match an open tab
    await db.persist_data()
    return DatastarResponse([
        SSE.patch_elements(
            selector=f"#tab-button-{tab_id}", mode=ElementPatchMode.REMOVE,
        ),
        SSE.patch_elements(
            selector=f"#tab-content-{tab_id}", mode=ElementPatchMode.REMOVE,
        ),
    ])
```

Per Non-goals §4: no validation that `tab_id`'s stored database matches `database`. The dep gates on `database`; that's authoritative. `remove_tab` is a no-op if the id is unknown.

## 4. Helpers removed

- `_promote_to_admin` — replaced by `SessionAdmin`.
- `_promote_to_db_admin` — replaced by `SessionDatabaseAdmin`.
- `_members_route_common` — no longer needed (no tab lookup; `panel_id` inlined as `tab_panel_id(tab_id)`).
- `_admin_panel_id` — was a one-liner around `tab_panel_id`; inlined.

Unchanged: `_re_render_members`, `_re_render_policies` — same shape, called with `db_session, panel_id` from the route.

## 5. Templates updated

Three partials add `&database={{ database | urlencode }}` to every URL they build. `database` is already in the template context for the manage page (passed by `render_manage`).

| Template | URLs to update |
|---|---|
| `authorization/_members_section.html` | 6 form-submit URLs (one per tier × user/group) and 1 revoke URL inside the per-member loop = 7 total |
| `authorization/_row_policies.html` | 1 form-submit URL (add policy) + 1 button URL inside the per-policy loop (revoke) = 2 |
| `authorization/_danger.html` | 1 form-submit URL (delete database) = 1 |

The `_admin_*.html` partials and `create_database.html` are unchanged — admin URLs don't take `database`, and create-database creates one.

`{{ database | urlencode }}` uses Jinja's built-in URL encoding for the (already-validated) database name. Server-side encoding here is simpler than the existing `+ encodeURIComponent($tabs.<id>.input)` JS pattern because `database` is server-known at render time, not client-bound.

## 6. Defense-in-depth still intact

The three layers from the original frontend-architecture spec are preserved:

1. **Nav rendering** (presentation): unchanged.
2. **Intent gate** (gateway): unchanged — `POST /api/tabs` still checks `IntentSpec.required` before opening a tab.
3. **Per-route guard** (authoritative): NOW visible in the route signature via `SessionAdmin` / `SessionDatabaseAdmin` / `SessionDatabaseCreator`. Each dep raises `AuthForbidden` (handled by the existing auth exception handler → 403) on cap failure.

The refactor does NOT change which HTTP status codes get returned for which failures (still 403 for cap failure, 400 for malformed input, 404 for unknown route). The response body for 403 may differ slightly because rejection now comes from `_require_*` (which raises `AuthForbidden` and goes through `install_exception_handlers`) instead of the route body's `HTTPException(403)`. Tests that assert on response body content for 403s need a one-line update.

## 7. Tests

### 7.1 Existing test deltas

For each test in `tests/features/test_authorization_*.py` that calls a manage route (members / policies / danger), add `database=<db>` to the `params=` dict. Example:

```python
# Before
r = client.post(
    "/feature/auth/MG12CD34/members/reader/user",
    params={"username": "bob"},
    headers=headers,
)

# After
r = client.post(
    "/feature/auth/MG12CD34/members/reader/user",
    params={"database": "marketing", "username": "bob"},
    headers=headers,
)
```

Tests asserting 403 from `_promote_to_db_admin` may see a slightly different response body now that the rejection comes from `AuthForbidden`. Status code unchanged (403). Update body assertions if any exist (most tests just check `r.status_code == 403`).

Affected files (audit by grep):

- `tests/features/test_authorization_members.py`
- `tests/features/test_authorization_row_policies.py`
- `tests/features/test_authorization_danger.py`
- `tests/features/test_authorization_audit.py` (if it touches the manage page route)
- `tests/features/test_authorization_manage.py` (the render-route test stays as-is)

### 7.2 Admin / create-database tests

`tests/features/test_authorization_admin_console.py` and `tests/features/test_authorization_create_database.py` need the same body-assertion review for any 403 cases (now rejected at the dep layer). No URL change.

### 7.3 New tests

None required. The dep behavior is already covered by the existing `tests/auth/` suite (`test_session_dep.py` etc.). The refactor wires existing deps into existing routes; no new code paths.

## 8. Files

| Path | Change |
|---|---|
| `src/iris/features/authorization/routes.py` | Refactor 21 routes to use typed `Session*` deps; delete 4 helpers; add `database: str` query param to 15 routes |
| `src/iris/features/authorization/templates/authorization/_members_section.html` | Add `&database={{ database | urlencode }}` to 7 URLs |
| `src/iris/features/authorization/templates/authorization/_row_policies.html` | Add `&database=...` to 2 URLs |
| `src/iris/features/authorization/templates/authorization/_danger.html` | Add `&database=...` to 1 URL |
| `tests/features/test_authorization_members.py` | Add `database=` to existing test params; verify 403 assertions |
| `tests/features/test_authorization_row_policies.py` | Same |
| `tests/features/test_authorization_danger.py` | Same |
| `tests/features/test_authorization_admin_console.py` | Verify 403 assertions (no URL change) |
| `tests/features/test_authorization_create_database.py` | Verify 403 assertions (no URL change) |

## 9. Risks and tradeoffs

- **Trust the URL.** As above (§Non-goals): if a malicious or buggy client passes inconsistent `tab_id` and `database`, the route operates on `database` and SSE-targets `tab_id`. The user has admin on `database` (gated by the dep), so the action is authorized; targeting a stale `tab_id` is a no-op client-side. Acceptable.
- **Larger query strings.** Each manage URL gains one parameter. Browser/proxy URL length limits are well above what we'd reach (typical caps ~2000 chars; our URLs stay under 200).
- **`AuthForbidden` body for 403.** Tests that hard-code expected 403 response body may need updating. Tests that only check status code are unaffected.
- **`intents.py` still has the inline-promotion smell.** Out of scope here; deferred to a follow-up spec (Option B from the brainstorm: `IntentSpec` gains `session_type`, dispatcher promotes upfront, handlers receive a typed session).
