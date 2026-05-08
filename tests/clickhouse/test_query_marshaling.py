"""Unit tests for the private CH HTTP-param marshaller."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest


def _import_marshal():
    from iris.clickhouse.queries import _marshal_param

    return _marshal_param


def test_marshal_bool_true_is_one():
    m = _import_marshal()
    assert m(True) == "1"


def test_marshal_bool_false_is_zero():
    m = _import_marshal()
    assert m(False) == "0"


def test_marshal_int_passes_through():
    m = _import_marshal()
    assert m(42) == "42"


def test_marshal_float_passes_through():
    m = _import_marshal()
    assert m(3.14) == "3.14"


def test_marshal_str_passes_through():
    m = _import_marshal()
    assert m("hello") == "hello"


def test_marshal_datetime_iso_no_tz_suffix():
    m = _import_marshal()
    dt = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)
    assert m(dt) == "2026-05-09T12:00:00"


def test_marshal_none_raises():
    m = _import_marshal()
    with pytest.raises(TypeError, match="unsupported CH param type: NoneType"):
        m(None)


def test_marshal_list_raises():
    m = _import_marshal()
    with pytest.raises(TypeError, match="unsupported CH param type: list"):
        m([1, 2, 3])
