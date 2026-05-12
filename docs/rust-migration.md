# Rust migration

Design notes for a hypothetical port of iris from FastAPI/Python to axum/Rust.
Captured from the planning conversation in May 2026. Status: **exploratory** —
no code has been written, no commitment made. The numbers and crate behaviors
below were measured against ClickHouse 26.4 and `clickhouse-rs` git HEAD
(post-0.13.3) on 2026-05-12.

## Scope and motivation

What the port would give us:

- Single static binary, no Python interpreter, no venv on the target host.
- Compile-time enforcement of the `Session*` tier model — currently a
  code-review rule, would become a type-system rule.
- Real concurrency (no GIL); a busy admin console + many SSE-streaming feature
  tabs share one process cleanly.
- The DDL-safety layer (`validate_identifier`, `quote_identifier`, reserved
  suffix rejection) becomes pure-string Rust code that's trivially audited.

What it would cost:

- Multi-week rewrite for one engineer (rough order of magnitude). Most of the
  surface area — auth providers, CH provisioning, the shell, the Authorization
  feature — has to be reimplemented, not mechanically translated.
- Loss of FastAPI/pydantic iteration speed during prototyping.
- Operational dependency on ClickHouse ≥ 26.4 (see [the EXECUTE AS section](#clickhouse-and-clickhouse-rs-requirements)).

Recommendation in the current state of the world: stay on Python unless
deployment footprint or per-process throughput becomes a real pain point. The
existing `basedpyright` + `ruff` gates plus the `Session*` discipline already
buy most of the safety wins the rewrite would add.

## Workspace layout

Multi-crate Cargo workspace, one feature per crate. The "no cross-feature
imports" rule from the Python codebase becomes a compiler-enforced invariant:
a feature crate that wants to import another would have to add it to
`Cargo.toml`, which is visible in review and can be CI-linted.

```
iris/
├── Cargo.toml                              # workspace root
└── crates/
    ├── iris-core/                          # shared types, errors, identifier validation
    ├── iris-auth/                          # session store, providers (OIDC/LDAP/mock)
    ├── iris-clickhouse/                    # CH bridge, tier roles, DDL safety, policies
    ├── iris-shell/                         # Contributions, tabs, template registry, base HTML
    ├── iris-feature-authorization/
    ├── iris-feature-<future>/
    └── iris-server/                        # binary; wires everything together
```

Workspace root pins shared deps once:

```toml
[workspace]
members = ["crates/*"]
resolver = "2"

[workspace.dependencies]
axum = "0.7"
tokio = { version = "1", features = ["full"] }
minijinja = "2"
clickhouse = { git = "https://github.com/ClickHouse/clickhouse-rs", rev = "<pin>" }
```

### Feature registration

Stay with explicit installation, mirroring iris's current `build_app` order
(auth → clickhouse → shell → features). The server binary is the one place
that knows the full feature list:

```rust
fn build_app(state: AppState) -> Router {
    let mut builder = AppBuilder::new(state);
    iris_auth::install(&mut builder);
    iris_clickhouse::install(&mut builder);
    iris_shell::install(&mut builder);
    iris_feature_authorization::install(&mut builder);
    builder.into_router()
}
```

Skip `inventory` / `linkme` auto-discovery. The explicit list is more
idiomatic and matches the existing pattern.

### Templates without a filesystem

Each feature crate embeds its templates at compile time with `include_dir!`,
registered with the shell's minijinja `Environment` from `install()`. The
final binary is one artifact; no runtime filesystem walk, no
"templates/ dir missing in this deployment" failure mode.

### When to split out a crate

Crates have per-crate compile overhead (codegen-unit setup, fresh metadata,
link step). Rule of thumb: split a feature out when it has its own
routes + templates + non-trivial logic + tests (Authorization clearly
qualifies). Trivial features stay as modules in an existing crate until they
grow up.

## Library choices

| Concern | Choice | Rationale |
|---|---|---|
| HTTP framework | `axum` | Tower-based, good extractor model, fits the `Session*` tier system naturally |
| Templates | `minijinja` | Armin Ronacher's port, near-1:1 Jinja2 syntax, runtime template registration matches iris's per-feature contribution model |
| ClickHouse client | `clickhouse` (HTTP) | Most actively maintained; per-call user/password override via cheap `Client::clone()`; cleanly handles `EXECUTE AS` on CH 26.4+ (see below) |
| Session store | `sqlx` + sqlite | Direct analog of the SQLite store iris uses today; async; `.sqlx-data.json` for offline query checking |
| LDAP | `ldap3` | Async, mature; closest analog to the current Python `ldap3` |
| OIDC | `openidconnect` | Full discovery/PKCE; pairs with `oauth2` |
| Datastar SDK | `datastar` (crate) | Provides axum extractor `ReadSignals<T>` and `Sse` response wrapper |
| Test containers | `testcontainers` + `testcontainers-modules` | ClickHouse module is prebuilt; Keycloak via `GenericImage` with `--import-realm` |
| Linker (Linux) | `mold` | Drop-in; cuts link time on 200-dep async projects from ~6s → 0.5s |
| Test runner | `cargo-nextest` | Process-per-test, faster scheduling, better failure output |
| Cross-run cache | `sccache` | Caches crate-level compilation; cuts cold-cache CI from ~3 min → ~30s |

Explicitly **not chosen**:

- `klickhouse` (native TCP) — faster and richer type fidelity, but smaller
  community and the `EXECUTE AS` story over native protocol is different
  enough that we'd need to redo the validation work. Revisit only if
  throughput-bound.
- `askama` for templates — compile-time type checking is nice, but it forces
  templates to be known at build time and bound to specific Rust structs,
  which breaks the per-feature dynamic template registry.
- `inventory` / `linkme` for feature auto-discovery — covered above.

## Authorization tier model

The `XxxSession` hierarchy maps onto axum extractors. The
`AuthSession` struct is `pub(crate)` to `iris-auth`; feature crates only see
the typed `SessionRead` / `SessionDatabaseAdmin` / `SessionAdmin` newtypes
and only get the capability methods their tier defines.

```rust
// Tier 1: any authenticated user.
pub struct SessionRead(AuthSession);

// Tier 2: admin of a specific database. Extractor pulls {database} from
// the matched path, validates the identifier, checks the tier, and stores
// the validated db on the session value.
pub struct SessionDatabaseAdmin {
    inner: AuthSession,
    pub database: String,
}

// Tier 3: global admin (bootstrap CLICKHOUSE_ADMIN_USER / GROUP).
pub struct SessionAdmin(AuthSession);
```

Each tier has its own `FromRequestParts` impl that:

1. Loads the auth session from the cookie.
2. (Where applicable) composes the `Path` extractor to read the database
   identifier from the URL.
3. Runs `validate_identifier` *during extraction* — so the handler receives
   a name that has already cleared the reserved-suffix and shape checks.
4. Runs the tier check (`is_dbadmin_of(&database)`,
   `is_global_admin()`).
5. Returns the tier struct or a typed rejection (401/403/400).

The capability surface is per-tier: `SessionRead` exposes
`query_as_user(&self, sql)`; `SessionDatabaseAdmin` exposes
`grant_reader(&self, target_user)`; `SessionAdmin` exposes
`create_database(&self, name)`. A feature module trying to call
`create_database` on a `SessionRead` is a compile error, not a code-review
catch.

### What this gives us over the Python version

- The "right tier, wrong database" bug class disappears: the database name
  on `SessionDatabaseAdmin` is the one the extractor validated; the handler
  can't grant against a different db.
- DDL-safety runs at the request boundary, before any handler code sees the
  identifier.
- The `_ch` private-field-access security violation becomes structurally
  impossible: features import `SessionRead` etc. from `iris-auth` and only
  get the methods on those newtypes.

## ClickHouse and clickhouse-rs requirements

iris uses ClickHouse's `EXECUTE AS <user> <inner_query>` for user
impersonation. The service user holds `GRANT IMPERSONATE`, and the SQL is
prepended at the bridge layer. This is the load-bearing primitive for the
tier model — get it wrong and either row policies don't apply (security) or
the admin console can't read system tables (functionality).

**Hard operational requirements:**

1. **ClickHouse ≥ 26.4** (released 2026-04-30). 26.4 fixes the bug where
   `EXECUTE AS` silently ignored `FORMAT` and `INTO OUTFILE` clauses
   specified in the inner query. On 26.3 and earlier, an impersonated query
   with an appended `FORMAT RowBinary` clause returns plain TSV instead.
2. **clickhouse-rs ≥ 0.14** (or a pinned git rev from `main` until 0.14
   ships). 0.13.3 has three bugs that combine to make `EXECUTE AS`
   unusable via the high-level `fetch_all::<T>()` API:
   - Uses `Method::GET` for read queries under 8192 bytes — CH treats GET
     as forcing readonly mode, which rejects `EXECUTE AS`.
   - Appends `?readonly=1` to the URL even on the POST path for typed
     fetches.
   - Appends ` FORMAT RowBinary` to the SQL string — which, combined with
     the CH 26.3 bug above, returns the wrong wire format.

   All three are fixed on `main` of the crate (commit 598ebb9 or later):
   POST is unconditional, the `readonly=1` insertion is gone, and the format
   goes via the `?default_format=` URL parameter rather than appended to
   the SQL.

**Verified working configuration** (measured via a captured proxy on
2026-05-12 against ClickHouse 26.4.2.10 and `clickhouse-rs` HEAD):

```rust
pub struct ClickHouseBridge { service: Client }

impl ClickHouseBridge {
    pub async fn query_as_user<T>(&self, user: &str, sql: &str) -> Result<Vec<T>>
    where T: Row + DeserializeOwned
    {
        let user_q = quote_identifier(user, Kind::Username)?;
        self.service
            .query(&format!("EXECUTE AS {user_q} {sql}"))
            .fetch_all::<T>()
            .await
    }

    pub async fn command_as_service(&self, sql: &str) -> Result<()> {
        self.service.query(sql).execute().await
    }
}
```

`Client` is `#[derive(Clone)]` with an `Arc<dyn HttpClient>`, so the
connection pool is shared across clones. If we later need per-user
client tuning (different timeouts, headers, etc.) `client.clone().with_*()`
is cheap.

Query parameters via `Query::param("name", value)` use ClickHouse's native
`{name:Type}` placeholder syntax and travel on the URL as `param_name=...`
— they resolve correctly inside the impersonated `EXECUTE AS` body.

**Pre-bootstrap requirement on the operator side:** the service user iris
authenticates as needs `GRANT IMPERSONATE ON *.* TO <iris_service> WITH
GRANT OPTION` (in addition to its existing access management grants). The
provisioning code in `iris-clickhouse` owns this grant.

**If we ship before either gate is available**, fall back to raw HTTP for
the `query_as_user` path only — the same shape iris uses today via httpx.
The rest of the bridge (`command_as_service`, service-user typed reads)
works on the high-level crate regardless.

## Test strategy

Pytest's session-scoped fixture model doesn't translate directly — Cargo
makes every file under `tests/` a separate binary, and Rust has no native
session-scope concept. The pragmatic shape:

### One integration test binary, shared containers

`crates/iris-server/tests/integration.rs` with `mod auth; mod clickhouse;
mod features;` submodules. Container handles live in a
`tokio::sync::OnceCell<Fixtures>` initialized lazily on first access. This
gives us the closest analog to today's session-scoped pytest containers.

```rust
static FIXTURES: OnceCell<Fixtures> = OnceCell::const_new();

pub async fn fixtures() -> &'static Fixtures {
    FIXTURES.get_or_init(|| async {
        // boot ClickHouse + Keycloak, seed bootstrap admin, return handles
    }).await
}
```

Per-test isolation stays UUID-prefixed (`prefix` analog), same discipline
as today.

### Don't translate every Python test

- HTTP-shape and end-to-end tests translate well — they describe behavior,
  not Python internals. Use them as the parity oracle.
- Unit tests of Python-internal helpers (`validate_identifier`, the session
  hierarchy) get rewritten against the Rust types — they don't carry over.
- The slick option, if we want it: keep the existing pytest suite running
  as a *black-box acceptance harness* against the Rust binary over HTTP.
  Reuses every existing assertion as a parity oracle without porting the
  test framework. Trade-off is it doesn't exercise enough of the
  DDL-safety / session-tier internals; for those, write native Rust unit
  tests.

### Container reuse during dev

Set `TESTCONTAINERS_REUSE=true` locally so containers persist between
`cargo test` invocations — large iteration win, doesn't affect CI behavior.

### Keycloak readiness

`testcontainers` `WaitFor::http(...)` against
`/realms/master/.well-known/openid-configuration` (not `/`), which only
returns 200 after the realm is fully loaded. Seed the realm via
`--import-realm` + bind-mounted `realm.json` rather than scripting the
admin API at test time.

## Compile-time / CI

For ~600 tests with the layout above, expected ballpark:

- Clean cold build, CI: ~2-4 min (mostly the ~150-200 dep tree).
- Incremental rebuild after touching one test file: 3-15s, mostly link.
- Incremental rebuild after touching `src/`: 10-30s.

Levers in order of impact:

1. **Library crate** (`iris-core`, `iris-auth`, etc.) — every test binary
   links against the prebuilt library; touching a test doesn't recompile
   iris itself. Single biggest factor.
2. **One integration test binary** instead of many — link cost amortizes.
3. **`mold` linker** on Linux (`apt install mold`; `.cargo/config.toml`
   sets `link-arg=-fuse-ld=mold`). 6s → 0.5s link on the test binary.
4. **`sccache` in CI** — cuts cold-cache CI from ~3 min to ~30s once warm.
5. **`cargo-nextest`** for running — doesn't affect compile time but cuts
   wall-clock test time ~40% via better parallelism.

Final-binary link time doesn't get faster with multi-crate splitting — the
server binary still links every feature crate. The win is purely
incremental: editing feature A doesn't dirty feature B.

## Deliberately deferred

The following came up and we deliberately did not make a decision:

- **Cross-feature integration through registries beyond `Contributions`**.
  The "don't add a new registry until two features need it" rule carries
  over verbatim. Adding registry types is cheap in Rust but each one is
  permanent API surface.
- **Native CH protocol (`klickhouse`).** Faster, richer types, but the
  `EXECUTE AS` validation work has only been done against HTTP. Revisit if
  throughput-bound.
- **Compile-time plugin discovery (`inventory` / `linkme`).** The
  explicit-install pattern matches Python iris's existing shape; switching
  to compile-time discovery should be driven by concrete need, not
  aesthetics.
- **Migration path from Python iris.** The realistic options are
  green-field (new repo, port feature-by-feature against parity tests) vs.
  strangler (run both servers behind a reverse proxy, route features one
  at a time). Choice depends on how much downtime tolerance there is and
  whether the Keycloak/CH bootstrap state can be shared.

## Verification artifacts

The `EXECUTE AS` + `clickhouse-rs` behavior table was produced by a small
Rust binary against a `testcontainers`-style `clickhouse-server` docker
container, with an HTTP logging proxy between the binary and ClickHouse to
capture the exact wire traffic. The test confirmed:

- On `clickhouse-rs` 0.13.3 + CH 26.3: idiomatic `fetch_all::<T>()` of an
  `EXECUTE AS` query fails three ways (GET-implies-readonly, readonly=1 on
  POST, FORMAT clause silently dropped by CH).
- On `clickhouse-rs` HEAD (598ebb9) + CH 26.4: identical `fetch_all::<T>()`
  call succeeds end-to-end, with bound parameters, returning the correct
  impersonated `currentUser()` value.

The scratch project is not preserved. To reproduce, the steps are: spin up
`clickhouse/clickhouse-server:26.4` with
`CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`, grant `IMPERSONATE ON *.*` to
the service user, create a target user, then build a small async-tokio
binary depending on `clickhouse = { git = "...", branch = "main" }` and
call `client.query("EXECUTE AS \`user\` ...").fetch_all::<T>().await`.
