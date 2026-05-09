import pytest

from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    policy_name,
    quote_identifier,
    quote_sql_array_element,
    quote_sql_literal,
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


def test_quote_sql_literal_wraps_plain_value():
    assert quote_sql_literal("EU") == "'EU'"


def test_quote_sql_literal_doubles_embedded_single_quotes():
    assert quote_sql_literal("O'Brien") == "'O''Brien'"


def test_quote_sql_literal_escapes_backslashes():
    assert quote_sql_literal(r"a\b") == r"'a\\b'"


def test_quote_sql_literal_handles_combined_escapes():
    # backslash must be escaped before quotes, otherwise '\\\'' would be ambiguous
    assert quote_sql_literal("a\\'b") == "'a\\\\''b'"


def test_quote_sql_array_element_wraps_plain_value():
    assert quote_sql_array_element("EU") == "'EU'"


def test_quote_sql_array_element_backslash_escapes_single_quote():
    """Inside a CH array literal, single quotes are backslash-escaped
    (NOT doubled — that grammar is rejected inside `[...]`)."""
    assert quote_sql_array_element("O'Brien") == "'O\\'Brien'"


def test_quote_sql_array_element_doubles_backslash():
    assert quote_sql_array_element(r"a\b") == r"'a\\b'"


def test_quote_sql_array_element_handles_combined_escapes():
    # Backslash doubled, then single quote backslash-escaped.
    assert quote_sql_array_element("a\\'b") == "'a\\\\\\'b'"


def test_quote_sql_array_element_empty_string():
    assert quote_sql_array_element("") == "''"


_RESERVED_SUFFIX_VALUES = ("_USER", "_GRP", "_DBADMIN", "_DBWRITER", "_DBREADER")


@pytest.mark.parametrize("suffix", _RESERVED_SUFFIX_VALUES)
def test_validate_identifier_rejects_reserved_suffix_for_database(suffix):
    with pytest.raises(InvalidIdentifierError, match=suffix):
        validate_identifier(f"foo{suffix}", kind="database")


@pytest.mark.parametrize("suffix", _RESERVED_SUFFIX_VALUES)
def test_validate_identifier_rejects_reserved_suffix_for_username(suffix):
    with pytest.raises(InvalidIdentifierError, match=suffix):
        validate_identifier(f"alice{suffix}", kind="username")


@pytest.mark.parametrize("suffix", _RESERVED_SUFFIX_VALUES)
def test_validate_identifier_rejects_reserved_suffix_for_group(suffix):
    with pytest.raises(InvalidIdentifierError, match=suffix):
        validate_identifier(f"sales{suffix}", kind="group")


def test_validate_identifier_accepts_reserved_suffix_for_role():
    """Tier role names like `<db>_DBADMIN` legitimately end in those
    suffixes; the check must not fire for kind='role'."""
    for suffix in _RESERVED_SUFFIX_VALUES:
        assert validate_identifier(f"foo{suffix}", kind="role") == f"foo{suffix}"


@pytest.mark.parametrize("kind", ["table", "column", "policy"])
def test_validate_identifier_accepts_reserved_suffix_for_other_kinds(kind):
    for suffix in _RESERVED_SUFFIX_VALUES:
        assert validate_identifier(f"foo{suffix}", kind=kind) == f"foo{suffix}"


def test_validate_identifier_accepts_normal_external_names():
    assert validate_identifier("alice", kind="username") == "alice"
    assert validate_identifier("sales", kind="group") == "sales"
    assert validate_identifier("orders", kind="database") == "orders"


def test_validate_identifier_error_message_mentions_offending_suffix():
    """Error text must include the suffix so operators tracing logs see why."""
    try:
        validate_identifier("alice_DBADMIN", kind="username")
    except InvalidIdentifierError as exc:
        msg = str(exc)
        assert "_DBADMIN" in msg, f"suffix not in error message: {msg!r}"
        assert "username" in msg, f"kind not in error message: {msg!r}"
    else:
        pytest.fail("expected InvalidIdentifierError")


def test_is_fixed_string_type_matches_expected_forms():
    """``is_fixed_string_type`` is the public predicate consumers go through.
    Matches `FixedString(N)` for any digit N, rejects everything else."""
    from iris.clickhouse.identifiers import is_fixed_string_type

    assert is_fixed_string_type("FixedString(16)") is True
    assert is_fixed_string_type("FixedString(1)") is True
    assert is_fixed_string_type("String") is False
    assert is_fixed_string_type("Nullable(String)") is False
    assert is_fixed_string_type("FixedString(N)") is False  # not a digit
    assert is_fixed_string_type("FixedString()") is False  # missing arg
    assert is_fixed_string_type("") is False


def test_policy_name_basic_shape():
    name = policy_name("orders", "lines", "writer", "EU")
    # <db>_<table>_<role>_<slug>_<16charhash>
    assert name.startswith("orders_lines_writer_EU_")
    suffix = name.split("_")[-1]
    assert len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in suffix)


def test_policy_name_uses_64_bit_digest():
    """Digest is 16 hex chars (64 bits). 32-bit collisions silently dropped
    the second policy via CREATE ROW POLICY IF NOT EXISTS."""
    name = policy_name("db", "t", "r", "any-value")
    digest = name.rsplit("_", 1)[-1]
    assert len(digest) == 16


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
        "derive_capabilities",
        "drop_tier_roles",
        "grant_insert_update_to_table",
        "grant_select_to_database",
        "grant_tier_to_group",
        "grant_tier_to_user",
        "provision_user",
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
