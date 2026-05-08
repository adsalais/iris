# Session as handle

Inline the ClickHouse handle methods onto the session value itself, with the available method surface determined by the session subclass returned from each tier alias. Routes take one parameter per access tier instead of two (alias + handle). The Python type system enforces that a route author cannot call a method outside the alias's tier — the missing methods don't exist on the returned class.

## Why

The CH-only authorization migration (`docs/superpowers/specs/2026-05-08-clickhouse-only-authz-design.md`) split route admission and route capability across two parameters:

```python
@app.get("/db/{database}/data")
async def read(
    database: str,
    session: SessionRead,                                          # admission
    handle: ClickHouseHandle = Depends(get_clickhouse_handle),     # capability
):
    return await handle.query_as_user("SELECT ...")
```

The two-parameter pattern has three problems:

1. **The route author has to remember to pair the right alias with the right handle.** Pairing `SessionRead` with `ClickHouseAdminHandle` would compile but admit a non-admin user to admin operations only because the handle dep would 403 them. The redundancy doesn't add safety; it adds a place for bugs.
2. **The handle capability isn't actually constrained by the alias.** A `SessionRead` route can request a `ClickHouseAdminHandle` — both deps run, the second 403s on tier mismatch. Defence in depth at the cost of a second exception path that should never fire.
3. **Routes are noisier than the model.** Every CH route needs two parameters even though there's exactly one access concept ("this user can do X to database Y").

Collapsing alias and handle into one Session subclass folds admission and capability into a single typed value. The IDE autocompletes only the methods the tier permits; calling a method the tier doesn't expose is a type error, not a runtime 403.

## Scope

In:
- New Session class hierarchy in `iris.auth.identity`: `AuthSession` (base), `DatabaseSession`, `DatabaseAdminSession`, `DatabaseCreatorSession`, `AdminSession`. The base class is called `AuthSession` (not `Session`) because `Session` is already the name of the `Annotated`-alias dep — they would collide.
- Refactor CH operation implementations (currently methods on `ClickHouseHandle` / `ClickHouseAdminHandle` / `ClickHouseDatabaseAdminHandle` / `ClickHouseDatabaseCreatorHandle`) into module-level functions in `iris.clickhouse.handle` taking primitive args.
- Session subclass methods are thin wrappers that lazy-import the standalone functions and call them with `self.client` / `self.http_client` / `self.user.username` / `self.database`.
- Update the alias deps in `iris.auth.deps` to construct the right Session subclass with CH client refs injected from `request.app.state`.
- Delete the four handle classes and the four handle-provider deps in `iris.clickhouse.deps`.
- Auto-scope `query_as_user` for database-bound sessions via CH's HTTP `?database=` URL parameter; expose an optional `database=` kwarg on `Session.query_as_user` / `AdminSession.query_as_user` / `AdminSession.query_as_service`.
- Update routes (the demo app and any tests with route definitions) to drop the second parameter.
- Test refactor: handle-class tests become Session-method tests.

Out:
- Re-derivation of rights mid-session, runtime tier checks (still type-only).
- Changes to `derive_rights`, `bootstrap_admin`, `init_user_rights`, tier-role helpers, row-policy helpers, audit functions — the standalone implementations stay where they are.
- Schema or RBAC behavioral changes.
- Provider, CSRF, rate-limit, exception-handler refactors.
- Backwards-compat shim. The just-merged migration was big-bang; this is a follow-up of the same shape.

## Decisions

### Class hierarchy

```
AuthSession                  # any logged-in user
├─ DatabaseSession           # bound to a database (AuthSession + database field)
│   └─ DatabaseAdminSession  # adds tier grants/revokes/delete/list_admin_members/audit
├─ DatabaseCreatorSession    # adds create_database(name)
└─ AdminSession              # adds query_as_service, reprovision_user, audit, row policies
```

Each class is a frozen `@dataclass(frozen=True, slots=True)`. Inheritance is supported by Python frozen dataclasses (each child must also be frozen). The `client` / `http_client` / `settings` fields are declared with `field(repr=False, compare=False)` so they don't appear in repr/equality but are otherwise normal dataclass fields. The dep resolver injects them at construction.

`DatabaseSession` is the "value type" returned by both `SessionRead` and `SessionWrite` aliases — the alias gates admission tier-correctly, but the value type is identical because no method on iris's surface is writer-only. CH itself enforces SELECT vs INSERT vs ALTER UPDATE on the actual SQL passed to `query_as_user`.

`AdminSession` does **not** include per-DB methods. A global admin who needs to do per-DB operations writes a route gated by `SessionDatabaseAdmin`, which admits admins via the `is_admin` superset and returns a `DatabaseAdminSession` bound to the path's database. Routes that need both global ops and per-DB ops compose two deps; this is rare enough that two parameters is acceptable.

### Alias-to-type mapping

| Alias | Returns | Methods |
|---|---|---|
| `Session` | `AuthSession` | `query_as_user(sql, parameters=None, database=None)` |
| `SessionOptional` | `AuthSession \| None` | same |
| `SessionRead` | `DatabaseSession` | `query_as_user(sql, parameters=None)` (auto-scoped to `self.database`) |
| `SessionWrite` | `DatabaseSession` | same |
| `SessionDatabaseAdmin` | `DatabaseAdminSession` | inherits `query_as_user`; adds `grant_reader/writer`, `add_admin_user`, `revoke_reader/writer`, `remove_admin_user`, group equivalents (`grant_reader_to_group` etc.), `delete_database`, `list_admin_members`, `list_grants`, `list_row_policies` |
| `SessionDatabaseCreator` | `DatabaseCreatorSession` | inherits `query_as_user`; adds `create_database(name)` |
| `SessionAdmin` | `AdminSession` | inherits `query_as_user`; adds `query_as_service(sql, parameters=None, database=None)`, `reprovision_user(username, groups)`, `add_row_policy`, `revoke_row_policy`, `user_grants(username)`, `role_grants(role)`, `user_role_memberships(username)`, `user_row_policies(username)`, `role_row_policies(role)`, `table_row_policies(database, table)` |

The admission logic stays where it is in `iris.auth.deps` (private resolver functions); the resolvers now construct the appropriate subclass.

### Database scoping for `query_as_user`

ClickHouse's HTTP interface accepts `?database=foo` as a URL parameter that sets the default schema for the query. Unqualified table names resolve against this default; fully-qualified names (`other_db.t`) ignore it. This lets DB-scoped sessions transparently scope queries:

```python
@app.get("/db/{database}/count")
async def count(database: str, session: SessionRead):
    return await session.query_as_user("SELECT count() FROM t")
    # → POST /?database=<database>&default_format=JSONEachRow
    # → CH resolves `t` against <database>
```

For `DatabaseSession` and its subclasses (`DatabaseAdminSession`), `query_as_user` takes no `database` argument — the bound `self.database` is the source of truth. Querying a different DB from a DB-scoped route requires a fully-qualified table name; CH enforces privileges on that name.

For `AuthSession` and `AdminSession`, `query_as_user` accepts an optional `database=None` kwarg. When supplied, the URL param is set; when `None`, the URL param is omitted and unqualified names resolve against CH's session-default. `AdminSession.query_as_service` accepts the same kwarg (`clickhouse-connect`'s `Client.query` already exposes `database=`, so the implementation passes through).

### Module placement

| Class / function | Lives in |
|---|---|
| `AuthSession`, `DatabaseSession`, `DatabaseAdminSession`, `DatabaseCreatorSession`, `AdminSession` | `iris.auth.identity` |
| `User`, `UserSession`, `Rights`, `EMPTY_RIGHTS`, `rights_to_dict`, `rights_from_dict` | unchanged |
| Standalone CH operation functions (`query_as_user_impl`, `grant_reader_impl`, `delete_database_impl`, etc.) | `iris.clickhouse.handle` (refactored from methods on the deleted handle classes) |
| Alias deps and resolvers (`Session`, `SessionRead`, …, `_require_*`, `_to_view`) | `iris.auth.deps` |
| Helpers used by Session methods (validation, role names, query building) | `iris.clickhouse.{identifiers,grants,policies,audit,users,bootstrap,rights,client,config}` (unchanged) |

The Session classes import the standalone CH functions **lazily inside method bodies** to avoid an `iris.auth → iris.clickhouse` import cycle at module load. The alias resolvers in `iris.auth.deps` likewise build sessions with refs from `app.state` rather than importing CH module-level state.

### Refactoring the CH handle implementations

The current handle classes (`ClickHouseHandle`, `ClickHouseAdminHandle`, `ClickHouseDatabaseCreatorHandle`, `ClickHouseDatabaseAdminHandle`) all hold `(client, http_client, settings, username[, database])` and run `asyncio.to_thread`-wrapped CH operations. The refactor extracts the body of each method as a module-level async function in `iris.clickhouse.handle`:

```python
# iris/clickhouse/handle.py — after refactor
async def query_as_user_impl(
    http_client: httpx.AsyncClient,
    *,
    username: str,
    sql: str,
    parameters: Mapping[str, Any] | None = None,
    database: str | None = None,
) -> list[dict[str, Any]]:
    body = f"EXECUTE AS {quote_identifier(username, kind='username')} {sql}"
    params: dict[str, str] = {"default_format": "JSONEachRow"}
    if database:
        params["database"] = database
    if parameters:
        for k, v in parameters.items():
            params[f"param_{k}"] = str(v)
    response = await http_client.post("/", params=params, content=body)
    response.raise_for_status()
    text = response.text.strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line]


async def grant_reader_impl(
    client: Client, *, database: str, username: str
) -> None:
    await asyncio.to_thread(
        grant_tier_to_user,
        client,
        database=database,
        tier=TIER_DBREADER,
        username=username,
    )

# … one async function per existing handle method …
```

The Session classes call these with `self.client` / `self.http_client` / `self.user.username` / `self.database` as appropriate. No state lives on the standalone functions; they're thin wrappers around the existing imperative helpers.

### Session class shape

```python
@dataclass(frozen=True, slots=True)
class AuthSession:
    """Any logged-in user. Has query_as_user; nothing else.

    The CH client refs are injected by the dep resolver and used only to
    talk to ClickHouse. They are not part of the session's persistent
    identity (which is captured by id/user/created_at/expires_at/data/
    rights — the same fields the SessionStore round-trips).
    """
    id: str
    user: User
    created_at: datetime
    expires_at: datetime
    data: dict[str, Any]
    rights: Rights
    client: Any = field(repr=False, compare=False)
    http_client: Any = field(repr=False, compare=False)
    settings: Any = field(repr=False, compare=False)

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        from iris.clickhouse.handle import query_as_user_impl
        return await query_as_user_impl(
            self.http_client,
            username=self.user.username,
            sql=sql,
            parameters=parameters,
            database=database,
        )


@dataclass(frozen=True, slots=True)
class DatabaseSession(AuthSession):
    database: str

    async def query_as_user(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        # Override: no `database` kwarg here — bound to self.database.
        from iris.clickhouse.handle import query_as_user_impl
        return await query_as_user_impl(
            self.http_client,
            username=self.user.username,
            sql=sql,
            parameters=parameters,
            database=self.database,
        )


@dataclass(frozen=True, slots=True)
class DatabaseAdminSession(DatabaseSession):
    async def grant_reader(self, username: str) -> None:
        from iris.clickhouse.handle import grant_reader_impl
        await grant_reader_impl(self.client, database=self.database, username=username)

    async def grant_writer(self, username: str) -> None: ...
    async def add_admin_user(self, username: str) -> None: ...
    async def revoke_reader(self, username: str) -> None: ...
    async def revoke_writer(self, username: str) -> None: ...
    async def remove_admin_user(self, username: str) -> None: ...
    async def grant_reader_to_group(self, group: str) -> None: ...
    async def grant_writer_to_group(self, group: str) -> None: ...
    async def add_admin_group(self, group: str) -> None: ...
    async def revoke_reader_from_group(self, group: str) -> None: ...
    async def revoke_writer_from_group(self, group: str) -> None: ...
    async def remove_admin_group(self, group: str) -> None: ...
    async def delete_database(self) -> None: ...
    async def list_admin_members(self) -> list[str]: ...
    async def list_grants(self) -> list[dict[str, Any]]: ...
    async def list_row_policies(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class DatabaseCreatorSession(AuthSession):
    async def create_database(self, name: str) -> None:
        from iris.clickhouse.handle import create_database_impl
        await create_database_impl(
            self.client,
            settings=self.settings,
            name=name,
            creator_username=self.user.username,
        )


@dataclass(frozen=True, slots=True)
class AdminSession(AuthSession):
    async def query_as_service(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        *,
        database: str | None = None,
    ) -> QueryResult: ...
    async def reprovision_user(self, *, username: str, groups: list[str]) -> None: ...
    async def grant_select_to_database(self, *, database: str, role: str) -> None: ...
    async def grant_insert_update_to_table(self, *, database: str, table: str, role: str) -> None: ...
    async def add_row_policy(
        self, *, database: str, table: str, column: str, role: str, value: str
    ) -> None: ...
    async def revoke_row_policy(
        self, *, database: str, table: str, role: str, value: str
    ) -> None: ...
    async def user_grants(self, *, username: str) -> list[dict[str, Any]]: ...
    async def role_grants(self, *, role: str) -> list[dict[str, Any]]: ...
    async def user_role_memberships(self, *, username: str) -> list[dict[str, Any]]: ...
    async def user_row_policies(self, *, username: str) -> list[dict[str, Any]]: ...
    async def role_row_policies(self, *, role: str) -> list[dict[str, Any]]: ...
    async def table_row_policies(
        self, *, database: str, table: str
    ) -> list[dict[str, Any]]: ...
```

### Dep resolver shape

The alias resolvers in `iris.auth.deps` build the right subclass by reading the CH refs off `request.app.state` (set by `iris.clickhouse.install`):

```python
def _ch_refs(request: Request) -> tuple[Any, Any, Any]:
    """Return (clickhouse_client, http_client, settings) or (None, None, None)
    when CH isn't installed (build_app(install_clickhouse=False))."""
    state = request.app.state
    return (
        getattr(state, "clickhouse_client", None),
        getattr(state, "clickhouse_http_client", None),
        getattr(state, "clickhouse_settings", None),
    )


async def _require_database_admin(
    request: Request, database: str, stored: _StoredSession
) -> DatabaseAdminSession:
    if stored is None:
        raise AuthRequired()
    if not stored.rights.has_admin(database):
        raise AuthForbidden(needed=(f"database_admin[{database}]",), have=())
    client, http_client, settings = _ch_refs(request)
    return DatabaseAdminSession(
        id=stored.id, user=stored.user, created_at=stored.created_at,
        expires_at=stored.expires_at, data=stored.data, rights=stored.rights,
        client=client, http_client=http_client, settings=settings,
        database=database,
    )
```

When CH isn't installed (auth-only test apps), `_ch_refs` returns `(None, None, None)`. Calling a CH method on such a session raises `AttributeError` from the underlying `httpx`/`Client` `None` reference — tests that don't exercise CH simply don't call those methods. Tests that do exercise CH inject mocks the same way they did for the old handle classes (now via the session's private fields).

### Route examples

```python
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, session: Session):
    return templates.TemplateResponse(request, "index.html", {"user": session.user})

@app.get("/db/{database}/count")
async def count(database: str, session: SessionRead):
    return await session.query_as_user("SELECT count() FROM t")

@app.post("/db/{database}")
async def create_db(database: str, session: SessionDatabaseCreator):
    await session.create_database(database)
    return {"created": database}

@app.post("/db/{database}/grants/users/{username}")
async def grant_read(database: str, username: str, session: SessionDatabaseAdmin):
    await session.grant_reader(username)
    return {"granted": True}

@app.delete("/db/{database}")
async def delete_db(database: str, session: SessionDatabaseAdmin):
    await session.delete_database()
    return {"deleted": database}

@app.get("/admin/users/{username}/grants")
async def audit(username: str, session: SessionAdmin):
    return await session.user_grants(username=username)

@app.get("/admin/probe/{db}")
async def probe(db: str, session: SessionAdmin):
    return await session.query_as_service("SELECT count() FROM t", database=db)
```

One parameter per route. The IDE shows only the methods the tier exposes.

### Failure modes

- **No session:** the alias dep raises `AuthRequired` (401), unchanged.
- **Tier mismatch:** the alias dep raises `AuthForbidden` (403), unchanged.
- **CH method called when CH isn't installed:** raises at the lazy import or on the first `await self._http_client.post(...)`. Tests that don't need CH don't reach this path; routes in production always have CH installed.
- **Route author calls a method outside the tier:** type error, not a runtime exception. The IDE / `basedpyright --level error` catches it before the code runs. There is no runtime fallback path.

The simplification is genuine: the only failure modes that survive are the two existing auth gates and the existing CH-itself failures (privilege errors on actual queries, network errors, etc.). The "wrong handle paired with wrong alias" failure mode disappears because there is no separate handle.

## Module map (post-change)

```
src/iris/auth/
├── __init__.py              # public surface unchanged in shape; new Session class names exported
├── session.py               # Rights, EMPTY_RIGHTS, rights_to_dict/from_dict — unchanged
├── identity.py              # User, UserSession + Session class hierarchy (NEW)
├── config.py                # unchanged
├── sessions.py              # unchanged (still round-trips id/user/data/rights)
├── exceptions.py            # unchanged
├── deps.py                  # alias deps — _require_* resolvers now build Session subclasses
├── csrf.py                  # unchanged
├── rate_limit.py            # unchanged
├── routes.py                # /api/whoami unchanged in behavior; install() unchanged
├── bootstrap.py             # unchanged
└── providers/               # unchanged
```

```
src/iris/clickhouse/
├── __init__.py              # drops the four handle classes + four require_* deps from public surface
├── audit.py                 # unchanged (still functional helpers)
├── bootstrap.py             # unchanged
├── client.py                # unchanged
├── config.py                # unchanged
├── deps.py                  # DELETED — handle providers are gone, no auth↔ch bridge module needed
├── grants.py                # unchanged
├── handle.py                # rewritten: standalone async functions instead of handle classes
├── identifiers.py           # unchanged
├── install.py               # unchanged surface (still wires post-login hook + CH state)
├── policies.py              # unchanged
├── rights.py                # unchanged
└── users.py                 # unchanged
```

Deleted:
- `iris.clickhouse.deps` module entirely (no more handle providers).
- The `ClickHouseHandle`, `ClickHouseAdminHandle`, `ClickHouseDatabaseCreatorHandle`, `ClickHouseDatabaseAdminHandle` classes from `iris.clickhouse.handle` — replaced by standalone functions.

## Tests

- `tests/clickhouse/test_handle.py` and `test_handle_integration.py` — repurposed to test the standalone functions and the Session methods (one suite per surface).
- `tests/clickhouse/test_creator_handle.py`, `test_admin_handle.py`, `test_tier_promotion.py` — updated to construct Session subclasses directly (with CH refs from the existing `ch_client` fixture) and call methods on them. Same coverage shape; replace `ClickHouseDatabaseAdminHandle(...)` with `DatabaseAdminSession(... database=db, client=ch_client, http_client=stub_http, settings=ch_settings)`.
- `tests/clickhouse/test_clickhouse_deps.py` — DELETED. The functionality (alias-dep admission) moves to `tests/auth/test_deps.py`, which already covers `Session/SessionOptional/SessionRead/SessionWrite/SessionDatabaseAdmin/SessionDatabaseCreator/SessionAdmin` admission. Add coverage for the CH-method dispatch (verify the right Session subclass is returned).
- `tests/auth/test_deps.py` — adds tests that the constructed session has the right type at runtime (`isinstance(session, DatabaseAdminSession)`) and that the right CH refs are wired in.
- `tests/auth/test_session_dep.py` — already exercises round-tripping; gains a small test that the new private fields don't break SessionStore (they're not persisted).

## Migration / rollout

Big-bang. The just-merged migration cleared the path; this collapses what remains. Operator-facing: nothing changes — env vars, schema, RBAC behavior all unchanged. Developer-facing: route signatures lose the `handle: … = Depends(…)` parameter and Session methods replace handle methods. The diff is mechanical for any route that already uses the alias deps.

There is no compat shim. A consuming PR that hasn't migrated would fail to import the deleted handle classes; that's the explicit signal to update.

## Open risks

- **Frozen dataclass + inheritance** is supported but fiddly: every subclass must re-declare `@dataclass(frozen=True, slots=True)`. We already do this for `User` / `UserSession` / `Rights`, so the pattern is established.
- **Lazy imports inside method bodies** add a tiny per-call overhead (one cached import lookup). Negligible at iris's scale.
- **Test mock injection** moves from "construct a handle with mock CH refs" to "construct a session with mock CH refs as fields". Same complexity, slightly different shape. Tests that constructed handles directly become tests that construct sessions directly.
- **DatabaseSession.query_as_user has no `database` override.** Edge case: an admin scoped to `finance` who wants to do an ad-hoc query against `hr` from inside a `SessionDatabaseAdmin` route must use a fully-qualified `hr.t` name in the SQL. CH enforces; iris doesn't model "admin of finance temporarily querying hr without overriding the URL param". Acceptable — admins who need cross-DB query routes can use `SessionAdmin` and pass `database=` explicitly.
