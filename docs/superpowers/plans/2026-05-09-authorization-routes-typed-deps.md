# Authorization routes — typed deps refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor 21 of the 22 routes in `src/iris/features/authorization/routes.py` to use the existing typed `Session*` deps so the capability requirement is visible in each route's signature.

**Architecture:** Three task groups, one per dep type. Add `database: str` as a query param to the 15 routes that need `SessionDatabaseAdmin`. Use `SessionAdmin` for admin-console routes and `SessionDatabaseCreator` for create-database submit. Three template partials gain `&database={{ database | urlencode }}` in the URLs they build. Four helpers (`_promote_to_admin`, `_promote_to_db_admin`, `_members_route_common`, `_admin_panel_id`) are deleted as their callers migrate.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, Datastar; existing `iris.auth.deps.Session*` aliases; pytest, basedpyright, ruff. No new dependencies.

---

## Approach note: this is a refactor, not new behavior

Tests already exist and pass. The task pattern is: make the route + template + test changes together as one coherent commit, then run the full sweep + gates to confirm equivalence. There are no failing-test-then-passing-test cycles — the tests pass before and after.

The only test-side adjustment per route is adding `database=<db>` to the `params=` dict on calls that hit a `SessionDatabaseAdmin` route. Status codes (403 / 404 / 400 / 200) are preserved by the refactor; tests that only check status code work unchanged. The `httpx.TestClient` default `Accept: */*` header means `_wants_html` is false and `_on_auth_forbidden` returns bare `Response(status_code=403)` (no body) — same observable shape as the current `HTTPException(status_code=403)` on the test side.

---

## File structure

### Modified files

| Path | Change |
|---|---|
| `src/iris/features/authorization/routes.py` | 21 routes refactored across 3 tasks; 4 helpers deleted; `database` query param added to 15 routes |
| `src/iris/features/authorization/templates/authorization/_members_section.html` | 7 URLs gain `&database={{ database \| urlencode }}` |
| `src/iris/features/authorization/templates/authorization/_row_policies.html` | 2 URLs gain `&database={{ database \| urlencode }}` |
| `src/iris/features/authorization/templates/authorization/_danger.html` | 1 URL gains `&database={{ database \| urlencode }}` |
| `tests/features/test_authorization_members.py` | Add `"database": <db>` to params on every existing test call |
| `tests/features/test_authorization_row_policies.py` | Same |
| `tests/features/test_authorization_danger.py` | Same |
| `tests/features/test_authorization_admin_console.py` | No URL change; verify 403 assertions still pass |
| `tests/features/test_authorization_create_database.py` | No URL change; verify 403 assertions still pass |
| `tests/features/test_authorization_audit.py` | Verify it still passes (touches manage render path, not action routes) |

No new files. No deleted files.

---

## Task 1 — 15 `SessionDatabaseAdmin` routes (members + policies + delete) + 3 templates + their tests

**Files:**
- Modify: `src/iris/features/authorization/routes.py` (12 members + 2 policies + 1 delete = 15 routes; helpers `_promote_to_db_admin`, `_members_route_common` deleted)
- Modify: `src/iris/features/authorization/templates/authorization/_members_section.html` (7 URLs)
- Modify: `src/iris/features/authorization/templates/authorization/_row_policies.html` (2 URLs)
- Modify: `src/iris/features/authorization/templates/authorization/_danger.html` (1 URL)
- Modify: `tests/features/test_authorization_members.py`
- Modify: `tests/features/test_authorization_row_policies.py`
- Modify: `tests/features/test_authorization_danger.py`

- [ ] **Step 1: Snapshot the test baseline (sanity check)**

```bash
uv run pytest tests/features/test_authorization_members.py tests/features/test_authorization_row_policies.py tests/features/test_authorization_danger.py -q
```
Expected: All pass at the current commit. (5 + 3 + 3 = 11 tests.)

- [ ] **Step 2: Update `_members_section.html` to add `database` query param**

In `src/iris/features/authorization/templates/authorization/_members_section.html`, the section currently iterates over three tiers (`reader`, `writer`, `admin`) and emits two ADD forms per tier (one for users, one for groups) plus a REVOKE button per existing member. Each URL becomes prefixed with `database=`.

Replace the file's contents with:

```html
<section id="{{ panel_id }}-members" class="iris-members-section">
  <h3>Members</h3>
  {% for tier_label, tier_key in [("Readers", "reader"), ("Writers", "writer"), ("Admins", "admin")] %}
  <div class="iris-members-tier">
    <h4>{{ tier_label }}</h4>
    <form data-on:submit="@post('/feature/auth/{{ tab_id }}/members/{{ tier_key }}/user?database={{ database | urlencode }}&amp;username=' + encodeURIComponent($tabs.{{ tab_id }}.{{ tier_key }}_user_input || ''))"
          class="iris-grant-form">
      <input type="text" placeholder="Add user…"
             data-bind="tabs.{{ tab_id }}.{{ tier_key }}_user_input">
      <button type="submit">+ add user</button>
    </form>
    <form data-on:submit="@post('/feature/auth/{{ tab_id }}/members/{{ tier_key }}/group?database={{ database | urlencode }}&amp;group=' + encodeURIComponent($tabs.{{ tab_id }}.{{ tier_key }}_group_input || ''))"
          class="iris-grant-form">
      <input type="text" placeholder="Add group…"
             data-bind="tabs.{{ tab_id }}.{{ tier_key }}_group_input">
      <button type="submit">+ add group</button>
    </form>
    <ul>
      {% for m in members[tier_key] %}
      <li>
        {% if m.kind == "role" %}group: {% endif %}{{ m.name }}
        <button class="iris-revoke"
                data-on:click="@delete('/feature/auth/{{ tab_id }}/members/{{ tier_key }}/{{ 'group' if m.kind == 'role' else 'user' }}?database={{ database | urlencode }}&amp;{{ 'group' if m.kind == 'role' else 'username' }}=' + encodeURIComponent({{ m.name | tojson }}))">
          revoke
        </button>
      </li>
      {% endfor %}
    </ul>
  </div>
  {% endfor %}
</section>
```

- [ ] **Step 3: Update `_row_policies.html` to add `database` query param**

In `src/iris/features/authorization/templates/authorization/_row_policies.html`, the section emits one revoke URL per existing policy plus one submit URL on the add form. Both gain `database=`.

Replace the file's contents with:

```html
<section id="{{ panel_id }}-policies" class="iris-policies-section">
  <h3>Row policies</h3>
  <ul>
    {% for p in row_policies %}
    <li>
      {{ p.table | default("?") }} ON role {{ p.short_name | default(p.name) | default("?") }}: {{ p.select_filter | default("?") }}
      <button data-on:click="@delete('/feature/auth/{{ tab_id }}/policies?database={{ database | urlencode }}&amp;table=' + encodeURIComponent({{ (p.table or '') | tojson }}) + '&amp;role=' + encodeURIComponent({{ (p.short_name or p.name or '') | tojson }}) + '&amp;value=' + encodeURIComponent({{ (p.select_filter or '') | tojson }}))">
        &times;
      </button>
    </li>
    {% endfor %}
  </ul>
  <form data-on:submit="@post('/feature/auth/{{ tab_id }}/policies?database={{ database | urlencode }}&amp;table=' + encodeURIComponent($tabs.{{ tab_id }}.policy_table || '') + '&amp;column=' + encodeURIComponent($tabs.{{ tab_id }}.policy_column || '') + '&amp;role=' + encodeURIComponent($tabs.{{ tab_id }}.policy_role || '') + '&amp;value=' + encodeURIComponent($tabs.{{ tab_id }}.policy_value || ''))"
        class="iris-add-policy">
    <input placeholder="table" data-bind="tabs.{{ tab_id }}.policy_table">
    <input placeholder="column" data-bind="tabs.{{ tab_id }}.policy_column">
    <input placeholder="role" data-bind="tabs.{{ tab_id }}.policy_role">
    <input placeholder="value" data-bind="tabs.{{ tab_id }}.policy_value">
    <button type="submit">+ add row policy</button>
  </form>
</section>
```

- [ ] **Step 4: Update `_danger.html` to add `database` query param**

Replace the file's contents with:

```html
<section id="{{ panel_id }}-danger" class="iris-danger-section">
  <h3>Danger</h3>
  <details>
    <summary class="iris-danger-toggle">delete database</summary>
    <form data-on:submit="@delete('/feature/auth/{{ tab_id }}/database?database={{ database | urlencode }}&amp;confirm=' + encodeURIComponent($tabs.{{ tab_id }}.delete_confirm || ''))">
      <p>Type <code>{{ database }}</code> to confirm. This action is irreversible.</p>
      <input placeholder="confirm db name"
             data-bind="tabs.{{ tab_id }}.delete_confirm">
      <button type="submit" class="iris-danger-submit">delete database</button>
    </form>
  </details>
</section>
```

- [ ] **Step 5: Refactor the 15 routes in `routes.py`**

Open `src/iris/features/authorization/routes.py`. Replace the section that defines `_promote_to_db_admin`, `_members_route_common`, and the 12 members + 2 policies + 1 delete routes (the section between the comment `# manage members — 12 routes …` and the comment `# admin_console — sub-tab GET routes`) with the following.

Two pieces of intent here:

1. Drop `_promote_to_db_admin` and `_members_route_common` entirely.
2. Each of the 15 routes now takes `db: SessionDatabaseAdmin` and `database: Annotated[str, Query(min_length=1, max_length=64)]`. `panel_id` is computed inline as `tab_panel_id(tab_id)`.

```python
# ---------------------------------------------------------------------------
# manage members — 12 routes ({reader,writer,admin} × {user,group} × {POST,DELETE})
# ---------------------------------------------------------------------------


async def _re_render_members(
    request: Request, db_session: DatabaseAdminSession, panel_id: str, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_members
    members = await list_members(db_session)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_members_section.html").render(
        panel_id=panel_id, tab_id=tab_id, members=members,
        database=db_session.database,
    )
    return DatastarResponse(
        SSE.patch_elements(
            html, selector=f"#{panel_id}-members", mode=ElementPatchMode.OUTER,
        ),
    )


# Reader user
@router.post("/{tab_id}/members/reader/user")
async def grant_reader_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.grant_reader(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/reader/user")
async def revoke_reader_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_reader(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Reader group
@router.post("/{tab_id}/members/reader/group")
async def grant_reader_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.grant_reader_to_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/reader/group")
async def revoke_reader_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_reader_from_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Writer user
@router.post("/{tab_id}/members/writer/user")
async def grant_writer_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.grant_writer(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/writer/user")
async def revoke_writer_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_writer(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Writer group
@router.post("/{tab_id}/members/writer/group")
async def grant_writer_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.grant_writer_to_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/writer/group")
async def revoke_writer_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_writer_from_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Admin user
@router.post("/{tab_id}/members/admin/user")
async def grant_admin_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.add_admin_user(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/admin/user")
async def revoke_admin_user(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    username: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.remove_admin_user(username)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# Admin group
@router.post("/{tab_id}/members/admin/group")
async def grant_admin_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.add_admin_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/members/admin/group")
async def revoke_admin_group(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    group: Annotated[str, Query(min_length=1, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.remove_admin_group(group)
    return await _re_render_members(request, db, tab_panel_id(tab_id), tab_id)


# ---------------------------------------------------------------------------
# manage row policies
# ---------------------------------------------------------------------------


async def _re_render_policies(
    request: Request, db_session: DatabaseAdminSession, panel_id: str, tab_id: str,
) -> Response:
    row_policies = await db_session.list_row_policies()
    templates = request.app.state.templates
    html = templates.get_template("authorization/_row_policies.html").render(
        panel_id=panel_id, tab_id=tab_id, row_policies=row_policies,
        database=db_session.database,
    )
    return DatastarResponse(
        SSE.patch_elements(
            html, selector=f"#{panel_id}-policies", mode=ElementPatchMode.OUTER,
        ),
    )


@router.post("/{tab_id}/policies")
async def add_policy(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    table: Annotated[str, Query(min_length=1, max_length=64)],
    column: Annotated[str, Query(min_length=1, max_length=64)],
    role: Annotated[str, Query(min_length=1, max_length=64)],
    value: Annotated[str, Query(min_length=0, max_length=4096)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.add_row_policy(table=table, column=column, role=role, value=value)
    return await _re_render_policies(request, db, tab_panel_id(tab_id), tab_id)


@router.delete("/{tab_id}/policies")
async def revoke_policy(
    request: Request, db: SessionDatabaseAdmin, tab_id: str,
    database: Annotated[str, Query(min_length=1, max_length=64)],
    table: Annotated[str, Query(min_length=1, max_length=64)],
    role: Annotated[str, Query(min_length=1, max_length=64)],
    value: Annotated[str, Query(min_length=0, max_length=4096)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    await db.revoke_row_policy(table=table, role=role, value=value)
    return await _re_render_policies(request, db, tab_panel_id(tab_id), tab_id)
```

Then find the `delete_database` route (currently around line 448, immediately after `_re_render_policies`) and replace it with:

```python
# ---------------------------------------------------------------------------
# danger zone — delete database
# ---------------------------------------------------------------------------


@router.delete("/{tab_id}/database")
async def delete_database(
    db: SessionDatabaseAdmin, tab_id: str,
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

Update the imports at the top of `routes.py` to add `SessionDatabaseAdmin` (since the bare `Session` is still used by the render route, keep both):

```python
from iris.auth.deps import Session, SessionDatabaseAdmin
```

Remove the now-unused `AuthSession` import:

```python
# Was: from iris.auth.views import AdminSession, AuthSession, DatabaseAdminSession
# Becomes (DatabaseAdminSession still needed for type hints in helpers, AdminSession still used by admin console for now):
from iris.auth.views import AdminSession, DatabaseAdminSession
```

- [ ] **Step 6: Update the three test files to pass `database=` in params**

In `tests/features/test_authorization_members.py`, every test that hits a `/members/{tier}/{user|group}` URL needs `"database": "marketing"` added to its `params=` dict (or whatever the test's `db` is — the existing tests use `database = "marketing"` via the seed). Concretely, the shared `_seed` helper at the top of the file already uses `"marketing"`. Each test currently has:

```python
r = client.post(
    "/feature/auth/MG12CD34/members/reader/user",
    params={"username": "bob"},
    headers=headers,
)
```

Becomes:

```python
r = client.post(
    "/feature/auth/MG12CD34/members/reader/user",
    params={"database": "marketing", "username": "bob"},
    headers=headers,
)
```

Same edit on the four test functions in this file: `test_grant_reader_user_returns_403_when_not_db_admin`, `test_grant_reader_user_returns_422_on_empty_username`, `test_grant_reader_user_calls_db_session_method`, `test_revoke_admin_group_calls_remove_admin_group`, and the CSRF check `test_grant_routes_csrf_required`.

In `tests/features/test_authorization_row_policies.py`, the same edit on every test: add `"database": "marketing"` to `params`. Three tests: `test_add_policy_403_when_not_db_admin`, `test_add_policy_calls_db_session_method`, `test_revoke_policy_calls_db_session_method`.

In `tests/features/test_authorization_danger.py`, the same: add `"database": "marketing"` to `params` on the three tests `test_delete_database_403_when_not_db_admin`, `test_delete_database_400_when_confirm_mismatches`, `test_delete_database_calls_method_and_closes_tab`.

A practical sed for each file (verify by grep before & after):

```bash
# tests/features/test_authorization_members.py
sed -i 's|params={"username": "bob"}|params={"database": "marketing", "username": "bob"}|' tests/features/test_authorization_members.py
sed -i 's|params={"group": "data-team"}|params={"database": "marketing", "group": "data-team"}|' tests/features/test_authorization_members.py
sed -i 's|params={"username": ""}|params={"database": "marketing", "username": ""}|' tests/features/test_authorization_members.py

# tests/features/test_authorization_row_policies.py
sed -i 's|params={"table": "events", "column": "user_id",\n                "role": "r1", "value": "alice"}|params={"database": "marketing", "table": "events", "column": "user_id", "role": "r1", "value": "alice"}|' tests/features/test_authorization_row_policies.py
# Multi-line sed is finicky — easier to open the file and use Edit on the three call sites.

# tests/features/test_authorization_danger.py
sed -i 's|params={"confirm": "marketing"}|params={"database": "marketing", "confirm": "marketing"}|' tests/features/test_authorization_danger.py
sed -i 's|params={"confirm": "wrong-name"}|params={"database": "marketing", "confirm": "wrong-name"}|' tests/features/test_authorization_danger.py
```

(The multi-line case in the row-policies file needs an `Edit` call rather than sed — three call sites, each two or three lines long.)

Verify by grep:

```bash
grep -n "params=" tests/features/test_authorization_members.py tests/features/test_authorization_row_policies.py tests/features/test_authorization_danger.py
```

Expected: every `params=` dict in these files now starts with `"database":`.

- [ ] **Step 7: Run the three test files**

```bash
uv run pytest tests/features/test_authorization_members.py tests/features/test_authorization_row_policies.py tests/features/test_authorization_danger.py -v
```
Expected: All pass (5 + 3 + 3 = 11 tests). Status codes unchanged from before.

- [ ] **Step 8: Run the full unit suite + gates**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero failures, zero issues.

- [ ] **Step 9: Commit**

```bash
git add src/iris/features/authorization/routes.py \
        src/iris/features/authorization/templates/authorization/_members_section.html \
        src/iris/features/authorization/templates/authorization/_row_policies.html \
        src/iris/features/authorization/templates/authorization/_danger.html \
        tests/features/test_authorization_members.py \
        tests/features/test_authorization_row_policies.py \
        tests/features/test_authorization_danger.py
git commit -m "$(cat <<'EOF'
refactor(features/authorization): manage routes use SessionDatabaseAdmin

15 routes (12 members + 2 row policies + 1 delete database) now declare
their authz requirement in the function signature via the existing
SessionDatabaseAdmin dep. They take a `database: str` query parameter
that FastAPI auto-injects into _require_database_admin, which raises
AuthForbidden on cap failure (handled by install_exception_handlers as
403, matching the previous status code).

Helpers _promote_to_db_admin and _members_route_common are deleted —
no longer used. The 4-line manual promotion in each route body
collapses to one or two lines of actual work.

Three template partials (_members_section, _row_policies, _danger) gain
&database={{ database | urlencode }} in the URLs they build. `database`
is already in the template context (passed by render_manage).

Tests get a mechanical update: every params= dict on a manage-route
call gains "database": "marketing". Status code assertions unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2 — 5 `SessionAdmin` routes (admin console + reprovision) + delete `_promote_to_admin`

**Files:**
- Modify: `src/iris/features/authorization/routes.py` (5 routes; helpers `_promote_to_admin`, `_admin_panel_id` deleted)
- Verify (no change expected): `tests/features/test_authorization_admin_console.py`

- [ ] **Step 1: Refactor the 5 admin-console routes**

In `src/iris/features/authorization/routes.py`, find the `# admin_console — sub-tab GET routes` section. Delete `_promote_to_admin` (the `def _promote_to_admin(session: AuthSession) -> AdminSession:` helper) and `_admin_panel_id` (the `def _admin_panel_id(tab_id: str) -> str: return tab_panel_id(tab_id)` one-liner). Replace the 5 routes (`admin_users`, `admin_databases`, `admin_policies`, `admin_audit`, `admin_reprovision_user`) with:

```python
@router.get("/{tab_id}/admin/users")
async def admin_users(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_all_users
    users = await list_all_users(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_users.html").render(
        panel_id=panel_id, tab_id=tab_id, users=users,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


@router.get("/{tab_id}/admin/databases")
async def admin_databases(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_all_databases
    databases = await list_all_databases(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_databases.html").render(
        panel_id=panel_id, tab_id=tab_id, databases=databases,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


@router.get("/{tab_id}/admin/policies")
async def admin_policies(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_all_row_policies
    policies = await list_all_row_policies(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_policies.html").render(
        panel_id=panel_id, tab_id=tab_id, policies=policies,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


@router.post("/{tab_id}/admin/users/{username}/reprovision")
async def admin_reprovision_user(
    request: Request, admin: SessionAdmin, tab_id: str, username: str,
    _: None = Depends(verify_csrf_header),
) -> Response:
    from iris.features.authorization.service import list_all_users
    # IdP groups aren't accessible from this code path; reprovision_user
    # rebuilds CH user identity + tier roles with empty groups.
    await admin.reprovision_user(username=username, groups=[])
    users = await list_all_users(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_users.html").render(
        panel_id=panel_id, tab_id=tab_id, users=users,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))


@router.get("/{tab_id}/admin/audit")
async def admin_audit(
    request: Request, admin: SessionAdmin, tab_id: str,
) -> Response:
    from iris.features.authorization.service import list_all_grants
    grants = await list_all_grants(admin)
    panel_id = tab_panel_id(tab_id)
    templates = request.app.state.templates
    html = templates.get_template("authorization/_admin_audit.html").render(
        panel_id=panel_id, tab_id=tab_id, grants=grants,
    )
    return DatastarResponse(SSE.patch_elements(
        html, selector=f"#{panel_id}-subtab", mode=ElementPatchMode.OUTER,
    ))
```

Update the imports at the top of `routes.py` to add `SessionAdmin` to the auth.deps line:

```python
from iris.auth.deps import Session, SessionAdmin, SessionDatabaseAdmin
```

- [ ] **Step 2: Run the admin-console tests**

```bash
uv run pytest tests/features/test_authorization_admin_console.py -v
```
Expected: All pass (12 tests — same as before the refactor; URLs unchanged, status codes unchanged).

- [ ] **Step 3: Run the full unit suite + gates**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero failures, zero issues.

- [ ] **Step 4: Commit**

```bash
git add src/iris/features/authorization/routes.py
git commit -m "$(cat <<'EOF'
refactor(features/authorization): admin console routes use SessionAdmin

5 routes (4 admin sub-tab GETs + reprovision) now declare their authz
requirement via the existing SessionAdmin dep. The is_admin gate moves
from the route body's _promote_to_admin call to the FastAPI dep
(_require_admin), which raises AuthForbidden → 403.

Helpers _promote_to_admin and _admin_panel_id are deleted — no longer
used. AdminSession import stays (used as the dep's typed return).

URLs and status codes unchanged. Tests pass without modification.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3 — `SessionDatabaseCreator` for the submit route

**Files:**
- Modify: `src/iris/features/authorization/routes.py` (1 route; clean up unused imports)
- Verify (no change expected): `tests/features/test_authorization_create_database.py`

- [ ] **Step 1: Refactor the submit route**

In `src/iris/features/authorization/routes.py`, find the `# create_database — submit handler` section. Replace `submit_create_database` with:

```python
@router.post("/{tab_id}/submit")
async def submit_create_database(
    request: Request,
    creator: SessionDatabaseCreator,
    tab_id: str,
    name: Annotated[str, Query(min_length=0, max_length=64)],
    _: None = Depends(verify_csrf_header),
) -> Response:
    from iris.shell.tabs import TabRecord, replace_tab

    rec = find_tab(creator.data, tab_id)
    if rec is None or rec.feature != "auth" or rec.intent != "create_database":
        raise HTTPException(status_code=404, detail="tab not found")

    templates = request.app.state.templates
    panel_id = tab_panel_id(tab_id)

    try:
        await creator.create_database(name)
    except (ValueError, RuntimeError) as e:
        # Re-render the form with the error inline. Validation errors
        # (InvalidIdentifierError <: ValueError) and CH-side errors all
        # surface as inline error fragments for the user to fix.
        html = templates.get_template("authorization/create_database.html").render(
            panel_id=panel_id, tab_id=tab_id, error=str(e),
        )
        return DatastarResponse(
            SSE.patch_elements(
                html, selector=f"#{panel_id}", mode=ElementPatchMode.OUTER,
            ),
        )

    # Success: re-target the existing tab to manage <new_db>.
    new_rec = TabRecord(
        id=tab_id, feature="auth", intent="manage",
        params={"database": name}, title=f"Manage {name}",
    )
    replace_tab(creator.data, tab_id, new_rec)
    await creator.persist_data()
    return DatastarResponse([
        SSE.patch_elements(
            templates.get_template("shell/_tab_strip.html").render(tab=new_rec.to_json()),
            selector=f"#tab-button-{tab_id}",
            mode=ElementPatchMode.OUTER,
        ),
        SSE.patch_elements(
            templates.get_template("shell/_tab_panel.html").render(tab=new_rec.to_json()),
            selector=f"#tab-content-{tab_id}",
            mode=ElementPatchMode.OUTER,
        ),
    ])
```

The body changes:
- `creator: SessionDatabaseCreator` replaces the `session: Session` plus the inline `if not (session.capabilities.is_admin or session.capabilities.can_create_database)` check (gone).
- `creator` is a `DatabaseCreatorSession` which inherits from `AuthSession`, so `creator.data` and `creator.persist_data()` work the same as `session.data` and `session.persist_data()`. The manual `DatabaseCreatorSession(...)` constructor call is gone.
- The inline `from iris.auth.views import DatabaseCreatorSession` import inside the function is removed (no longer needed).

Update the imports at the top of `routes.py`:

```python
from iris.auth.deps import Session, SessionAdmin, SessionDatabaseAdmin, SessionDatabaseCreator
```

Remove `DatabaseAdminSession` from the `iris.auth.views` import line if it's no longer needed for type hints (it's still needed in `_re_render_members` and `_re_render_policies` signatures, so KEEP it). Same for `AdminSession` (still used as the typed return of SessionAdmin in route signatures — but actually `SessionAdmin` is `Annotated[AdminSession, ...]` so the type is implied; you don't need to import `AdminSession` explicitly unless you reference it elsewhere). Verify with `ruff check` after the change — it'll flag any unused imports.

- [ ] **Step 2: Run the create-database tests**

```bash
uv run pytest tests/features/test_authorization_create_database.py -v
```
Expected: All pass (7 tests — same as before; URLs unchanged, status codes unchanged).

- [ ] **Step 3: Run the full unit suite + gates**

```bash
uv run pytest --ignore=tests/auth/integration --ignore=tests/clickhouse/integration -q
uv run ruff check
uv run basedpyright --level error
uv run basedpyright --level warning
```
Expected: zero failures, zero issues. If ruff flags `AdminSession` or `DatabaseAdminSession` as unused, remove from the import line.

- [ ] **Step 4: Final regression — run the integration suite too (optional but recommended)**

```bash
uv run pytest tests/clickhouse/integration tests/auth/integration -q
```
Expected: 23 integration tests pass (the existing `test_row_policy_filters_reader_but_not_admin` exercises the scalar `add_row_policy` path through `AdminSession.add_row_policy`, which we did not change — but a regression check at this point catches anything subtle).

- [ ] **Step 5: Commit**

```bash
git add src/iris/features/authorization/routes.py
git commit -m "$(cat <<'EOF'
refactor(features/authorization): create_database submit uses SessionDatabaseCreator

The submit_create_database route now declares its authz requirement
via the existing SessionDatabaseCreator dep. The is_admin/
can_create_database gate moves from the route body to
_require_database_creator, which raises AuthForbidden → 403.

The manual DatabaseCreatorSession(...) constructor call inside the route
is gone — the dep returns the typed session directly. The route uses
creator.data and creator.persist_data() (DatabaseCreatorSession inherits
AuthSession). URLs and status codes unchanged.

This completes the routes.py typed-deps refactor: 21 of 22 routes now
declare their authz in the function signature. The 22nd (the render
route) keeps Session because dispatch is dynamic — that refactor is
deferred to a separate intents.py spec.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Recap

3 tasks, 3 commits. End state:

- 21 of 22 routes use the typed `Session*` deps; capability requirement is in the function signature.
- 4 helpers deleted: `_promote_to_admin`, `_promote_to_db_admin`, `_members_route_common`, `_admin_panel_id`.
- 3 templates updated to include `database` in the URLs they emit.
- Test files updated mechanically (params= gains "database": ...); status code assertions unchanged.
- Render route stays as `Session` (dispatch-on-intent); deferred refactor is intents.py Option B.
