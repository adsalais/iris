import pytest

from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    quote_identifier,
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
