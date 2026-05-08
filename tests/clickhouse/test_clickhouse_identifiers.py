import pytest

from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    policy_name,
    quote_identifier,
    quote_string,
    validate_identifier,
)


def test_validate_identifier_accepts_alphanumeric_underscore():
    assert validate_identifier("alice", kind="username") == "alice"
    assert validate_identifier("user_42", kind="username") == "user_42"
    assert validate_identifier("ABC", kind="role") == "ABC"


def test_validate_identifier_rejects_empty_string():
    with pytest.raises(InvalidIdentifierError, match="username"):
        validate_identifier("", kind="username")


def test_validate_identifier_rejects_dash_dot_space():
    for bad in ("a-b", "a.b", "a b", "a/b", "a`b", "a;b"):
        with pytest.raises(InvalidIdentifierError, match="role"):
            validate_identifier(bad, kind="role")


def test_validate_identifier_kind_appears_in_error_message():
    with pytest.raises(InvalidIdentifierError, match=r"invalid database: 'has space'"):
        validate_identifier("has space", kind="database")


def test_quote_identifier_backticks_a_valid_name():
    assert quote_identifier("alice", kind="username") == "`alice`"


def test_quote_identifier_rejects_invalid_input():
    with pytest.raises(InvalidIdentifierError):
        quote_identifier("a b", kind="role")


def test_quote_string_wraps_plain_value():
    assert quote_string("EU") == "'EU'"


def test_quote_string_doubles_embedded_single_quotes():
    assert quote_string("O'Brien") == "'O''Brien'"


def test_quote_string_escapes_backslashes():
    assert quote_string(r"a\b") == r"'a\\b'"


def test_quote_string_handles_combined_escapes():
    # backslash must be escaped before quotes, otherwise '\\\'' would be ambiguous
    assert quote_string("a\\'b") == "'a\\\\''b'"


def test_policy_name_basic_shape():
    name = policy_name("orders", "lines", "writer", "EU")
    # <db>_<table>_<role>_<slug>_<8charhash>
    assert name.startswith("orders_lines_writer_EU_")
    suffix = name.split("_")[-1]
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)


def test_policy_name_distinct_for_distinct_values_with_same_slug():
    a = policy_name("db", "t", "r", "EU/UK")
    b = policy_name("db", "t", "r", "EU UK")
    # Slug strips both '/' and ' ' to '_', producing the same prefix...
    assert a.startswith("db_t_r_EU_UK_")
    assert b.startswith("db_t_r_EU_UK_")
    # ...but the trailing hash disambiguates.
    assert a != b


def test_policy_name_validates_identifier_arguments():
    with pytest.raises(InvalidIdentifierError):
        policy_name("bad-db", "t", "r", "EU")
    with pytest.raises(InvalidIdentifierError):
        policy_name("db", "bad table", "r", "EU")
    with pytest.raises(InvalidIdentifierError):
        policy_name("db", "t", "bad role", "EU")


def test_policy_name_handles_empty_or_only_special_value():
    name = policy_name("db", "t", "r", "!!!")
    # Slug of '!!!' is empty after stripping; substitute the placeholder 'v' and
    # rely on the hash to make it unique.
    assert name.startswith("db_t_r_v_")


def test_public_surface_exports_named_symbols():
    import iris.clickhouse as ch

    expected = {
        "ClickHouseSettings",
        "GLOBAL_ADMIN_ROLE",
        "TIER_DBADMIN",
        "TIER_DBREADER",
        "TIER_DBWRITER",
        "add_row_policy",
        "bootstrap_admin",
        "build_client",
        "create_tier_roles",
        "derive_rights",
        "drop_tier_roles",
        "grant_insert_update_to_table",
        "grant_select_to_database",
        "grant_tier_to_group",
        "grant_tier_to_user",
        "init_user_rights",
        "revoke_row_policy",
        "revoke_tier_from_group",
        "revoke_tier_from_user",
        "role_grants",
        "role_row_policies",
        "table_row_policies",
        "tier_role_name",
        "user_grants",
        "user_role_memberships",
        "user_row_policies",
    }
    assert set(ch.__all__) == expected
    for name in expected:
        assert hasattr(ch, name), name
