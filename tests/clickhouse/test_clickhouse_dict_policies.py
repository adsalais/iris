"""Tests for add_row_dict_policy + revoke_row_dict_policy.

Includes both the unit tests (assert on system.row_policies state) and the
end-to-end test (assert that a real CH user with the role sees the right
filtered rows) — both use the same ch_client testcontainer fixture from
tests/clickhouse/conftest.py.
"""
from __future__ import annotations

import pytest

from iris.clickhouse.bootstrap import GLOBAL_ADMIN_ROLE
from iris.clickhouse.grants import TIER_DBADMIN, tier_role_name
from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    dict_policy_name,
)
from iris.clickhouse.policies import (
    add_row_dict_policy,
    add_row_policy,
    revoke_row_dict_policy,
)


def _setup_protected_table(ch_client, db, table, role, auth_id_col="auth_id"):
    """Create a database, a protected table with id + region + auth_id columns,
    the role to gate, and the iris-synthesized roles the wildcards target."""
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
    ch_client.command(
        " ".join((
            f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}`",
            f"(id UInt64, region String, `{auth_id_col}` String)",
            "ENGINE = MergeTree ORDER BY id",
        ))
    )
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role}`")
    dba_role = tier_role_name(db, TIER_DBADMIN)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{dba_role}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{GLOBAL_ADMIN_ROLE}`")


def _setup_dict(ch_client, ch_settings, dict_db, dict_name):
    """Create a dict source table + dict in ``dict_db``. Caller fills the table
    and reloads the dict separately.

    The CH SOURCE clause carries the iris_svc credentials so the dict load
    does not fall back to the password-protected ``default`` user.
    """
    ch_client.command(f"CREATE DATABASE IF NOT EXISTS `{dict_db}`")
    ch_client.command(
        " ".join((
            f"CREATE TABLE IF NOT EXISTS `{dict_db}`.`{dict_name}_src`",
            "(`key` String, `authorisations` Array(String))",
            "ENGINE = MergeTree ORDER BY `key`",
        ))
    )
    source = (
        f"SOURCE(CLICKHOUSE(USER '{ch_settings.user}' "
        + f"PASSWORD '{ch_settings.password}' "
        + f"DB '{dict_db}' TABLE '{dict_name}_src'))"
    )
    ch_client.command(
        " ".join((
            f"CREATE DICTIONARY IF NOT EXISTS `{dict_db}`.`{dict_name}`",
            "(`key` String, `authorisations` Array(String))",
            "PRIMARY KEY `key`",
            source,
            "LAYOUT(COMPLEX_KEY_HASHED())",
            "LIFETIME(MIN 0 MAX 0)",
        ))
    )


def test_add_row_dict_policy_creates_named_policy_and_two_wildcards(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_dpol"
    table = "t"
    role = f"{prefix}_reader_dpol"
    dict_db = f"{prefix}_dicts"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, ch_settings, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )

    expected_name = dict_policy_name(
        db, table, role, "public", f"{dict_db}.{dict_name}",
        "authorisations", "auth_id",
    )
    expected_global_admin_wildcard = f"{db}_{table}_{GLOBAL_ADMIN_ROLE}"
    expected_dbadmin_wildcard = f"{db}_{table}_{tier_role_name(db, TIER_DBADMIN)}"

    rows = list(
        ch_client.query(
            "SELECT short_name FROM system.row_policies "
            + "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["short_name"] for r in rows}
    assert expected_name in names
    assert expected_global_admin_wildcard in names
    assert expected_dbadmin_wildcard in names


def test_add_row_dict_policy_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_dpol2"
    table = "t"
    role = f"{prefix}_reader_dpol2"
    dict_db = f"{prefix}_dicts2"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, ch_settings, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="v",
    )
    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="v",
    )
    count = next(
        ch_client.query(
            "SELECT count() AS c FROM system.row_policies "
            + "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )["c"]
    assert count == 3


def test_add_row_dict_policy_validates_inputs(ch_client, ch_settings):
    def _call(**overrides):
        kwargs = dict(
            database="d", table="t", auth_id="auth_id",
            dictionary="dict", authorisations="authorisations",
            role="r", value="v",
        )
        kwargs.update(overrides)
        return add_row_dict_policy(ch_client, **kwargs)

    with pytest.raises(InvalidIdentifierError):
        _call(database="bad-db")
    with pytest.raises(InvalidIdentifierError):
        _call(table="bad-table")
    with pytest.raises(InvalidIdentifierError):
        _call(auth_id="bad-col")
    with pytest.raises(InvalidIdentifierError):
        _call(role="bad-role")
    with pytest.raises(InvalidIdentifierError):
        _call(authorisations="bad-attr")
    with pytest.raises(InvalidIdentifierError):
        _call(dictionary="a.b.c")


def test_add_row_dict_policy_wildcards_no_op_when_scalar_already_present(
    ch_client, ch_settings, prefix
):
    """Locks in §5.2 of the spec: when add_row_policy seeded the
    iris_global_admin + <db>_DBADMIN wildcards on a table, a subsequent
    add_row_dict_policy on the same table must NOT duplicate or replace
    them — IF NOT EXISTS makes the wildcard CREATEs no-ops.
    """
    db = f"{prefix}_dpol3"
    table = "t"
    role_scalar = f"{prefix}_scalar_reader"
    role_dict = f"{prefix}_dict_reader"
    dict_db = f"{prefix}_dicts3"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role_scalar)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role_dict}`")
    _setup_dict(ch_client, ch_settings, dict_db, dict_name)

    add_row_policy(
        ch_client,
        database=db, table=table,
        column="region", role=role_scalar, value="EU",
    )

    def _wildcard_rows():
        return list(
            ch_client.query(
                """
                SELECT short_name, id, select_filter
                FROM system.row_policies
                WHERE database = {d:String} AND table = {t:String}
                  AND short_name IN (
                    {ga:String}, {dba:String}
                  )
                ORDER BY short_name
                """,
                parameters={
                    "d": db, "t": table,
                    "ga": f"{db}_{table}_{GLOBAL_ADMIN_ROLE}",
                    "dba": f"{db}_{table}_{tier_role_name(db, TIER_DBADMIN)}",
                },
            ).named_results()
        )

    before = _wildcard_rows()
    assert len(before) == 2

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role_dict, value="public",
    )

    after = _wildcard_rows()
    assert len(after) == 2
    assert [(r["short_name"], r["id"], r["select_filter"]) for r in before] \
        == [(r["short_name"], r["id"], r["select_filter"]) for r in after]


def test_revoke_row_dict_policy_drops_named_policy(ch_client, ch_settings, prefix):
    db = f"{prefix}_drev"
    table = "t"
    role = f"{prefix}_reader_drev"
    dict_db = f"{prefix}_dicts_drev"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, ch_settings, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    revoke_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    expected_name = dict_policy_name(
        db, table, role, "public", f"{dict_db}.{dict_name}",
        "authorisations", "auth_id",
    )
    rows = list(
        ch_client.query(
            "SELECT short_name FROM system.row_policies "
            + "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["short_name"] for r in rows}
    assert expected_name not in names


def test_revoke_row_dict_policy_does_not_drop_wildcards(
    ch_client, ch_settings, prefix
):
    db = f"{prefix}_drev2"
    table = "t"
    role = f"{prefix}_reader_drev2"
    dict_db = f"{prefix}_dicts_drev2"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, ch_settings, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    revoke_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    rows = list(
        ch_client.query(
            "SELECT short_name FROM system.row_policies "
            + "WHERE database = {d:String} AND table = {t:String}",
            parameters={"d": db, "t": table},
        ).named_results()
    )
    names = {r["short_name"] for r in rows}
    assert f"{db}_{table}_{GLOBAL_ADMIN_ROLE}" in names
    assert f"{db}_{table}_{tier_role_name(db, TIER_DBADMIN)}" in names


def test_revoke_row_dict_policy_is_idempotent(ch_client, ch_settings, prefix):
    db = f"{prefix}_drev3"
    table = "t"
    role = f"{prefix}_reader_drev3"
    dict_db = f"{prefix}_dicts_drev3"
    dict_name = "auth_map"
    _setup_protected_table(ch_client, db, table, role)
    _setup_dict(ch_client, ch_settings, dict_db, dict_name)

    add_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    revoke_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )
    revoke_row_dict_policy(
        ch_client,
        database=db, table=table, auth_id="auth_id",
        dictionary=f"{dict_db}.{dict_name}", authorisations="authorisations",
        role=role, value="public",
    )


def test_dict_policy_filters_real_user_query_end_to_end(
    ch_client, ch_settings, prefix
):
    """End-to-end: dict-keyed policy actually filters rows for the user's role.

    Mirrors the brainstorm experiment: protected table with auth_id col, dict
    that maps auth_id values to lists of tags, three policies (one per role)
    each gating a different tag, three users in distinct roles. Asserts each
    user sees only the rows their tag authorises.
    """
    db = f"{prefix}_filt"
    table = "t"
    dict_db = f"{prefix}_filt_dicts"
    dict_name = "auth_map"
    role_pub = f"{prefix}_pub"
    role_eu = f"{prefix}_eu"
    role_secret = f"{prefix}_secret"
    user_pub = f"{prefix}_alice"
    user_eu = f"{prefix}_bob"
    user_secret = f"{prefix}_eve"

    _setup_protected_table(ch_client, db, table, role_pub)
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role_eu}`")
    ch_client.command(f"CREATE ROLE IF NOT EXISTS `{role_secret}`")
    _setup_dict(ch_client, ch_settings, dict_db, dict_name)

    ch_client.command(
        f"INSERT INTO `{dict_db}`.`{dict_name}_src` VALUES "
        + "('rec-1', ['public']), "
        + "('rec-2', ['internal', 'eu_team']), "
        + "('rec-3', ['eu_team']), "
        + "('rec-4', ['secret'])"
    )
    ch_client.command(f"SYSTEM RELOAD DICTIONARY `{dict_db}`.`{dict_name}`")

    ch_client.command(
        f"INSERT INTO `{db}`.`{table}` (id, region, auth_id) VALUES "
        + "(10, 'eu', 'rec-1'), (20, 'eu', 'rec-2'), "
        + "(30, 'eu', 'rec-3'), (40, 'eu', 'rec-4')"
    )

    user_pw = "test-pw"
    for user, role in (
        (user_pub, role_pub),
        (user_eu, role_eu),
        (user_secret, role_secret),
    ):
        ch_client.command(
            f"CREATE USER `{user}` IDENTIFIED BY '{user_pw}' "
            + f"DEFAULT ROLE `{role}`"
        )
        ch_client.command(f"GRANT SELECT ON `{db}`.`{table}` TO `{role}`")
        ch_client.command(
            f"GRANT dictGet ON `{dict_db}`.`{dict_name}` TO `{role}`"
        )

    for role, value in (
        (role_pub, "public"),
        (role_eu, "eu_team"),
        (role_secret, "secret"),
    ):
        add_row_dict_policy(
            ch_client,
            database=db, table=table, auth_id="auth_id",
            dictionary=f"{dict_db}.{dict_name}",
            authorisations="authorisations",
            role=role, value=value,
        )

    # Query as each user via the HTTP interface (clickhouse_connect over
    # HTTP). Use httpx with explicit basic auth so there's no ambiguity
    # about which user we're authenticating as.
    import httpx

    base_url = f"http://{ch_settings.host}:{ch_settings.port}"

    def _ids_for(user: str) -> list[int]:
        r = httpx.post(
            base_url,
            params={"query": f"SELECT id FROM `{db}`.`{table}` ORDER BY id"},
            auth=(user, user_pw),
        )
        r.raise_for_status()
        return [int(line) for line in r.text.strip().splitlines() if line]

    assert _ids_for(user_pub) == [10]
    assert _ids_for(user_eu) == [20, 30]
    assert _ids_for(user_secret) == [40]
