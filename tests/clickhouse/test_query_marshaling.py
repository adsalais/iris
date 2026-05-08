"""Unit tests for the CH HTTP-param marshaller and SQL placeholder parser."""
from __future__ import annotations

from datetime import UTC, date, datetime, timezone

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


def _import_marshal():
    from iris.clickhouse.queries import _marshal_param

    return _marshal_param


# ---- String ---------------------------------------------------------------


def test_marshal_string_passes_through():
    m = _import_marshal()
    assert m("hello", "String") == "hello"


def test_marshal_string_preserves_unicode_and_quotes():
    m = _import_marshal()
    assert m("O'Brien — élan", "String") == "O'Brien — élan"


def test_marshal_fixed_string_passes_through():
    m = _import_marshal()
    assert m("abcdefgh", "FixedString(8)") == "abcdefgh"


# ---- Integers -------------------------------------------------------------


@pytest.mark.parametrize("ch_type", ["Int8", "Int16", "Int32", "Int64"])
def test_marshal_signed_int(ch_type):
    m = _import_marshal()
    assert m(-7, ch_type) == "-7"
    assert m(0, ch_type) == "0"


@pytest.mark.parametrize("ch_type", ["UInt8", "UInt16", "UInt32", "UInt64"])
def test_marshal_unsigned_int(ch_type):
    m = _import_marshal()
    assert m(42, ch_type) == "42"


def test_marshal_int_rejects_bool():
    """bool is a Python int subclass; the int handlers must reject it
    before the isinstance(int) branch swallows True/False as 1/0 strings."""
    m = _import_marshal()
    with pytest.raises(TypeError, match="bool"):
        m(True, "Int32")
    with pytest.raises(TypeError, match="bool"):
        m(False, "UInt8")


# ---- Floats ---------------------------------------------------------------


@pytest.mark.parametrize("ch_type", ["Float32", "Float64"])
def test_marshal_float(ch_type):
    m = _import_marshal()
    assert m(3.14, ch_type) == "3.14"


def test_marshal_float_accepts_int():
    m = _import_marshal()
    assert m(42, "Float64") == "42"


def test_marshal_float_rejects_bool():
    m = _import_marshal()
    with pytest.raises(TypeError, match="bool"):
        m(True, "Float64")


# ---- Bool -----------------------------------------------------------------


def test_marshal_bool_true():
    m = _import_marshal()
    assert m(True, "Bool") == "true"


def test_marshal_bool_false():
    m = _import_marshal()
    assert m(False, "Bool") == "false"


# ---- Date / Date32 --------------------------------------------------------


@pytest.mark.parametrize("ch_type", ["Date", "Date32"])
def test_marshal_date_from_date_value(ch_type):
    m = _import_marshal()
    assert m(date(2026, 5, 9), ch_type) == "2026-05-09"


@pytest.mark.parametrize("ch_type", ["Date", "Date32"])
def test_marshal_date_from_datetime_value(ch_type):
    m = _import_marshal()
    assert m(datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC), ch_type) == "2026-05-09"


# ---- DateTime / DateTime('TZ') --------------------------------------------


def test_marshal_datetime_naive_treated_as_utc():
    m = _import_marshal()
    assert m(datetime(2026, 5, 9, 12, 0, 0), "DateTime") == "2026-05-09 12:00:00"


def test_marshal_datetime_aware_utc():
    m = _import_marshal()
    assert m(datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC), "DateTime") == (
        "2026-05-09 12:00:00"
    )


def test_marshal_datetime_aware_non_utc_converts_to_utc():
    m = _import_marshal()
    plus2 = timezone(__import__("datetime").timedelta(hours=2))
    # 14:00 +02:00 == 12:00 UTC.
    assert m(datetime(2026, 5, 9, 14, 0, 0, tzinfo=plus2), "DateTime") == (
        "2026-05-09 12:00:00"
    )


def test_marshal_datetime_with_timezone_arg_uses_same_handler():
    m = _import_marshal()
    assert m(datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC), "DateTime('UTC')") == (
        "2026-05-09 12:00:00"
    )


def test_marshal_datetime_truncates_subsecond_for_plain_datetime():
    """Plain DateTime has second precision; sub-second is dropped."""
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 0, 0, 789000, tzinfo=UTC)
    assert m(val, "DateTime") == "2026-05-09 12:00:00"


# ---- DateTime64(p) --------------------------------------------------------


def test_marshal_datetime64_3_preserves_milliseconds():
    """The original bug: DateTime64(3) needs '.789'; the old code dropped it."""
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 789000, tzinfo=UTC)
    assert m(val, "DateTime64(3)") == "2026-05-09 12:34:56.789"


def test_marshal_datetime64_6_preserves_microseconds():
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 123456, tzinfo=UTC)
    assert m(val, "DateTime64(6)") == "2026-05-09 12:34:56.123456"


def test_marshal_datetime64_0_drops_fractional():
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 999999, tzinfo=UTC)
    assert m(val, "DateTime64(0)") == "2026-05-09 12:34:56"


def test_marshal_datetime64_3_truncates_microseconds():
    """Microsecond precision higher than the declared (3) gets truncated."""
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 789999, tzinfo=UTC)
    # 789999 us -> 789 ms (truncates)
    assert m(val, "DateTime64(3)") == "2026-05-09 12:34:56.789"


def test_marshal_datetime64_9_pads_with_zeros():
    """DateTime64(9) wants 9 digits; Python only has 6 us, so pad with zeros."""
    m = _import_marshal()
    val = datetime(2026, 5, 9, 12, 34, 56, 123456, tzinfo=UTC)
    assert m(val, "DateTime64(9)") == "2026-05-09 12:34:56.123456000"


# ---- Array(T) -------------------------------------------------------------


def test_marshal_array_of_int():
    m = _import_marshal()
    assert m([1, 2, 3], "Array(Int32)") == "[1,2,3]"


def test_marshal_array_of_string_quotes_each_element():
    m = _import_marshal()
    assert m(["alice", "bob"], "Array(String)") == "['alice','bob']"


def test_marshal_array_of_string_escapes_quote_and_backslash():
    m = _import_marshal()
    assert m(["O'Brien"], "Array(String)") == "['O\\'Brien']"
    assert m(["with\\backslash"], "Array(String)") == "['with\\\\backslash']"


def test_marshal_array_accepts_tuple():
    m = _import_marshal()
    assert m((1, 2), "Array(Int32)") == "[1,2]"


def test_marshal_array_empty():
    m = _import_marshal()
    assert m([], "Array(Int32)") == "[]"


def test_marshal_array_of_date_raises():
    """Date inside Array would emit unquoted in CH array literal — invalid syntax.
    Until quoting for Date/DateTime in arrays is implemented, fail loudly."""
    m = _import_marshal()
    with pytest.raises(TypeError, match="Array\\(Date\\) is not supported"):
        m([date(2026, 5, 9)], "Array(Date)")


def test_marshal_array_of_datetime_raises():
    m = _import_marshal()
    with pytest.raises(TypeError, match="Array\\(DateTime\\) is not supported"):
        m([datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)], "Array(DateTime)")


def test_marshal_array_of_datetime64_raises():
    m = _import_marshal()
    with pytest.raises(TypeError, match=r"Array\(DateTime64\(3\)\) is not supported"):
        m(
            [datetime(2026, 5, 9, 12, 34, 56, 789000, tzinfo=UTC)],
            "Array(DateTime64(3))",
        )


def test_marshal_array_of_nullable_date_raises():
    """Even Nullable(Date) inside Array hits the same restriction."""
    m = _import_marshal()
    with pytest.raises(TypeError, match=r"Array\(Nullable\(Date\)\) is not supported"):
        m([date(2026, 5, 9), None], "Array(Nullable(Date))")


# ---- Nullable(T) ----------------------------------------------------------


def test_marshal_nullable_none_is_NULL():
    m = _import_marshal()
    assert m(None, "Nullable(String)") == "NULL"
    assert m(None, "Nullable(Int32)") == "NULL"


def test_marshal_nullable_value_falls_through_to_inner():
    m = _import_marshal()
    assert m("hello", "Nullable(String)") == "hello"
    assert m(42, "Nullable(Int32)") == "42"


# ---- Combined wrappers ----------------------------------------------------


def test_marshal_array_of_nullable_int():
    m = _import_marshal()
    assert m([1, None, 3], "Array(Nullable(Int32))") == "[1,NULL,3]"


def test_marshal_nullable_array():
    """Nullable(Array(T)) unwraps the Nullable first."""
    m = _import_marshal()
    assert m(None, "Nullable(Array(Int32))") == "NULL"
    assert m([1, 2], "Nullable(Array(Int32))") == "[1,2]"


# ---- Unsupported types ----------------------------------------------------


def test_marshal_unknown_type_raises():
    m = _import_marshal()
    with pytest.raises(TypeError, match="unsupported CH param type"):
        m(b"\\x00", "Decimal(10, 2)")


def test_marshal_unsupported_python_value_raises():
    """Even with String, a non-string value is rejected."""
    m = _import_marshal()
    with pytest.raises(TypeError):
        m(42, "String")
