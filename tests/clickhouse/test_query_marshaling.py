"""Unit tests for the CH HTTP-param marshaller and SQL placeholder parser."""
from __future__ import annotations

import pytest


def _import_parse():
    from iris.clickhouse.queries import _parse_placeholder_types

    return _parse_placeholder_types


def test_parse_basic_placeholder():
    p = _import_parse()
    assert p("SELECT * FROM t WHERE x = {x:Int32}") == {"x": "Int32"}


def test_parse_multiple_placeholders():
    p = _import_parse()
    assert p("WHERE x = {x:Int32} AND s = {s:String}") == {
        "x": "Int32",
        "s": "String",
    }


def test_parse_nested_type_array_of_nullable():
    p = _import_parse()
    assert p("WHERE xs IN ({xs:Array(Nullable(Int32))})") == {
        "xs": "Array(Nullable(Int32))"
    }


def test_parse_nested_type_array_of_string():
    p = _import_parse()
    assert p("WHERE names IN ({names:Array(String)})") == {
        "names": "Array(String)"
    }


def test_parse_datetime64_with_precision():
    p = _import_parse()
    assert p("WHERE ts = {ts:DateTime64(3)}") == {"ts": "DateTime64(3)"}


def test_parse_datetime_with_timezone_arg():
    p = _import_parse()
    assert p("WHERE ts = {ts:DateTime('UTC')}") == {"ts": "DateTime('UTC')"}


def test_parse_repeated_name_same_type_is_one_entry():
    p = _import_parse()
    sql = "WHERE a = {u:String} OR b = {u:String}"
    assert p(sql) == {"u": "String"}


def test_parse_repeated_name_conflicting_types_raises():
    p = _import_parse()
    sql = "WHERE a = {u:String} OR b = {u:Int32}"
    with pytest.raises(ValueError, match="conflicting CH types for placeholder 'u'"):
        p(sql)


def test_parse_no_placeholders_is_empty():
    p = _import_parse()
    assert p("SELECT 1") == {}


def test_parse_trims_whitespace_inside_type():
    p = _import_parse()
    # CH accepts whitespace inside parametric types like Decimal(10, 2);
    # we trim leading/trailing only, preserve interior.
    assert p("WHERE x = {x: Int32 }") == {"x": "Int32"}
