# Remove `_grant_full_admin`'s testcontainer-specific fallback — design

**Date:** 2026-05-09
**Status:** approved, ready for implementation plan

## Context

`iris.clickhouse.bootstrap._grant_full_admin` carries a try/except that
catches the CH testcontainer's missing `NAMED COLLECTION ADMIN`
privilege and falls back to `GRANT CURRENT GRANTS ON *.*`:

```python
def _grant_full_admin(client: Client, *, role_q: str) -> None:
    """``GRANT ALL ON *.* WITH GRANT OPTION``, with a ``CURRENT GRANTS`` fallback
    for the testcontainer's missing NAMED COLLECTION ADMIN privilege."""
    try:
        client.command(f"GRANT ALL ON *.* TO {role_q} WITH GRANT OPTION")
    except DatabaseError as err:
        if "NAMED COLLECTION ADMIN" not in str(err):
            raise
        client.command(
            f"GRANT CURRENT GRANTS ON *.* TO {role_q} WITH GRANT OPTION"
        )
```

This is test-specific code in production. Two problems:

- **String-matching the CH error message is fragile** — any CH release
  that rewords the error breaks the fallback silently.
- **It teaches production code about the testcontainer's privilege
  shape**, which is the wrong direction of dependency.

The testcontainer image's `test` user lacks `NAMED COLLECTION ADMIN`,
so iris_svc (which currently inherits from `test` via `CURRENT GRANTS`)
also lacks it. Production iris connects as a real superuser and never
hits the fallback.

## Goal

Remove the try/except. `_grant_full_admin` should be a one-liner:
`client.command(f"GRANT ALL ON *.* TO {role_q} WITH GRANT OPTION")`.
Keep iris's behavior unchanged in production. Move the testcontainer
accommodation entirely into `tests/clickhouse/conftest.py`.

## Non-goals

- Refactoring `bootstrap_admin` to grant a curated privilege list
  instead of `GRANT ALL` (option C in the brainstorm). Larger change,
  out of scope.
- Touching `OAuthProvider._http_transport`, `build_app(install_clickhouse=False)`,
  or `MockProvider`. Those are deliberate test/dev seams, not hacks.
- Changing how iris's admin role is bootstrapped at the iris level.
  The role still gets `GRANT ALL ON *.* WITH GRANT OPTION`; only the
  path to that point changes for the test environment.

---

## Architecture

### Current test setup (problem)

`tests/clickhouse/conftest.py` connects as the testcontainer's `test`
user (XML-defined, privileged but lacking server-scope rarities) and
runs ~10 specific GRANTs on `iris_svc`, ending with
`GRANT CURRENT GRANTS ON *.* TO iris_svc WITH GRANT OPTION` as the
catch-all. iris_svc gets exactly what `test` has, which is everything
except `NAMED COLLECTION ADMIN` (and possibly other rarities).

When iris's `bootstrap_admin._grant_full_admin` runs as iris_svc and
issues `GRANT ALL ON *.* TO admin_role`, CH rejects it because
iris_svc doesn't have `NAMED COLLECTION ADMIN` to grant. The fallback
then issues `GRANT CURRENT GRANTS`, which delegates the privileges
iris_svc *does* have.

### Target test setup

Connect as the testcontainer's `default` user (no password) instead.
With `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1` (already set by the
fixture), `default` is the SQL access-management superuser — it can
issue any GRANT, including ones for privileges it doesn't itself
hold-with-grant-option, because access management is the meta-privilege
to issue access DDL.

`default` runs:

```sql
CREATE USER iris_svc IDENTIFIED BY 'iris_svc_pw';
GRANT ALL ON *.* TO iris_svc WITH GRANT OPTION;
```

The single `GRANT ALL` subsumes every individual GRANT the current
conftest enumerates (CREATE ROLE, ROLE ADMIN, CREATE USER, SELECT on
system.*, IMPERSONATE, CREATE DATABASE, etc.) plus `NAMED COLLECTION
ADMIN`.

### Production-side change

`bootstrap.py` becomes:

```python
def _grant_full_admin(client: Client, *, role_q: str) -> None:
    """``GRANT ALL ON *.* WITH GRANT OPTION`` to the iris admin role."""
    client.command(f"GRANT ALL ON *.* TO {role_q} WITH GRANT OPTION")
```

The `DatabaseError` import becomes unused — remove it from
`bootstrap.py`'s imports.

---

## Files touched

| File | Change |
|---|---|
| `tests/clickhouse/conftest.py` | Replace the `test`-user-driven iris_svc grant enumeration (currently ~25 lines of explicit grants) with a single `default`-user-driven `GRANT ALL ON *.* TO iris_svc WITH GRANT OPTION`. |
| `src/iris/clickhouse/bootstrap.py` | Drop the try/except in `_grant_full_admin`. Remove the `from clickhouse_connect.driver.exceptions import DatabaseError` import (no longer used here). |

---

## Test plan

### What proves the change works

The existing iris suite is the proof. Specifically:

- `tests/clickhouse/test_bootstrap_admin.py` — runs `bootstrap_admin`
  directly and asserts the admin role + iris_global_admin grants
  materialize. With the fallback removed, this test only passes if
  `_grant_full_admin`'s plain `GRANT ALL` succeeds — which is exactly
  the contract change we're making.
- `tests/clickhouse/test_install.py` — exercises the install path that
  calls `bootstrap_admin`. Same constraint.
- `tests/clickhouse/test_login_provisioning.py` — the post-login hook
  chain depends on iris_svc's privileges; if conftest's GRANT ALL was
  insufficient, init_user_rights / derive_rights would fail.
- `tests/clickhouse/integration/*` — the full role-and-policy graph
  built from the new conftest.

If the entire CH suite passes after the change, the test setup is
sufficient.

### Risk-prove-it test

Add a small smoke test that asserts iris_svc actually holds
`NAMED COLLECTION ADMIN` after the conftest setup:

```python
def test_iris_svc_has_named_collection_admin(ch_client):
    """Regression: iris_svc must hold NAMED COLLECTION ADMIN so
    bootstrap_admin's GRANT ALL succeeds. If the testcontainer's
    default-user setup ever changes and stops giving iris_svc this
    privilege, this test fires before the bootstrap test does."""
    rows = ch_client.query(
        "SELECT count() FROM system.grants "
        "WHERE user_name = 'iris_svc' AND access_type = 'NAMED COLLECTION ADMIN'"
    ).result_rows
    assert rows[0][0] >= 1, "iris_svc lost NAMED COLLECTION ADMIN"
```

This lives in a new `tests/clickhouse/test_conftest_grants.py` file
(unique basename per the project convention).

---

## Risks

- **`default` user might not actually hold `NAMED COLLECTION ADMIN`.**
  With `CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`, `default` becomes the
  SQL access-management user, which (per CH docs) can grant any
  privilege via DDL. Empirically: if it can grant ALL successfully,
  iris_svc gets the full set including NAMED COLLECTION ADMIN. The
  smoke test pinned above catches the failure case loudly. **If the
  fixture-side change fails when run** — i.e., the implementation plan
  hits a "default user can't grant ALL" error — the fallback option is
  to keep the long enumeration AND add a one-liner that grants
  `NAMED COLLECTION ADMIN` via `default` to either `test` or directly
  to `iris_svc`. Either way, no production code change.
- **Hidden coupling on iris_svc's exact privilege set.** Other tests
  may have implicitly relied on iris_svc *lacking* a privilege (e.g.,
  to test that some operation is forbidden). I checked the suite and
  no such test exists today: every CH test runs as iris_svc and
  expects success on its operations. Granting ALL doesn't break those.
- **Conftest cleanup might churn unrelated tests.** Replacing 25 lines
  of explicit grants with one GRANT ALL changes nothing observable per
  test. The smoke test pins the privilege.

---

## Out of scope (deferred)

- Refactoring `bootstrap_admin` to grant a curated privilege list
  (option C from the brainstorm). Cleaner long-term but bigger.
- Auditing other deliberate test seams (`_http_transport`,
  `install_clickhouse=False`, `MockProvider`). They're documented and
  not problematic.
- Removing the `KeycloakHandle` ↔ `KC_HOSTNAME_STRICT=false` shim in
  the Keycloak fixture; that's analogous to this issue but in a
  different subsystem and the tradeoff hasn't been re-litigated.
