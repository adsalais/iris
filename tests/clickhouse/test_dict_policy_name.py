"""Unit tests for dict_policy_name + validate_dict_name."""
from __future__ import annotations

import pytest

from iris.clickhouse.identifiers import (
    InvalidIdentifierError,
    dict_policy_name,
    validate_dict_name,
)


def test_dict_policy_name_format():
    name = dict_policy_name(
        database="marketing", table="events", role="readers_GRP", value="EU",
        dictionary="iris_dicts.auth_map",
        authorisations="authorisations", auth_id="auth_id",
    )
    assert name.startswith("marketing_events_readers_GRP_EU_")
    suffix = name.rsplit("_", 1)[-1]
    assert len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in suffix)


def test_dict_policy_name_distinct_for_different_dictionaries():
    n1 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict1", authorisations="a", auth_id="ai",
    )
    n2 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict2", authorisations="a", auth_id="ai",
    )
    assert n1 != n2


def test_dict_policy_name_distinct_for_different_attrs():
    n1 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict", authorisations="attr1", auth_id="ai",
    )
    n2 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict", authorisations="attr2", auth_id="ai",
    )
    assert n1 != n2


def test_dict_policy_name_distinct_for_different_auth_ids():
    n1 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict", authorisations="a", auth_id="auth_id_1",
    )
    n2 = dict_policy_name(
        database="d", table="t", role="r", value="v",
        dictionary="dict", authorisations="a", auth_id="auth_id_2",
    )
    assert n1 != n2


def test_dict_policy_name_validates_db_table_role():
    with pytest.raises(InvalidIdentifierError):
        dict_policy_name(
            database="d-bad", table="t", role="r", value="v",
            dictionary="dict", authorisations="a", auth_id="ai",
        )


def test_validate_dict_name_accepts_simple_name():
    assert validate_dict_name("auth_dict") == "auth_dict"


def test_validate_dict_name_accepts_db_dot_dict():
    assert validate_dict_name("iris_dicts.auth_dict") == "iris_dicts.auth_dict"


def test_validate_dict_name_rejects_more_than_one_dot():
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name("a.b.c")


def test_validate_dict_name_rejects_garbage_segments():
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name("good.bad-segment")
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name("bad-segment.good")
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name("")
    with pytest.raises(InvalidIdentifierError):
        validate_dict_name(".")
