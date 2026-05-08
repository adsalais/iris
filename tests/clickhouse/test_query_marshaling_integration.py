"""CH-roundtrip integration test for the typed parameter marshaller.

Builds a table covering every supported CH type, inserts known values,
runs typed parameterized SELECTs through CH's HTTP endpoint, and asserts
each query returns the expected row.

Bypasses ``EXECUTE AS`` — the test exercises the marshaller pipeline
directly via the same HTTP endpoint ``query_as_user`` uses, without
needing a separate test user with IMPERSONATE granted.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime

import httpx
import pytest

from iris.clickhouse.queries import _marshal_param, _parse_placeholder_types


async def _query_typed(
    http: httpx.AsyncClient,
    *,
    sql: str,
    parameters: dict[str, object],
) -> list[dict[str, object]]:
    """Mirror of query_as_user without the EXECUTE AS prefix.

    Same parser + marshaller pipeline; pure HTTP call against CH.
    """
    type_map = _parse_placeholder_types(sql)
    params: dict[str, str] = {"default_format": "JSONEachRow"}
    for k, v in parameters.items():
        params[f"param_{k}"] = _marshal_param(v, type_map[k])
    response = await http.post("/", params=params, content=sql)
    response.raise_for_status()
    text = response.text.strip()
    if not text:
        return []
    return [json.loads(line) for line in text.splitlines() if line]


@pytest.fixture
def http_client(ch_settings):
    """A factory that builds a fresh httpx.AsyncClient pointed at the CH
    testcontainer.

    Uses iris_svc credentials from ch_settings — the same identity that
    holds CREATE/INSERT/SELECT on the test table.

    The factory returns a new client per call so each test creates and
    tears down its transport inside its own ``asyncio.run()`` event loop;
    sharing one ``AsyncClient`` across multiple ``asyncio.run()`` calls
    breaks because httpx binds the transport sockets to whatever loop ran
    the first request.
    """
    base_url = f"http://{ch_settings.host}:{ch_settings.port}"

    def _make() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url,
            auth=(ch_settings.user, ch_settings.password),
            timeout=httpx.Timeout(30.0),
        )

    return _make


@pytest.fixture
def marshal_table(ch_client, prefix):
    """Per-test table with one column per supported CH type, plus two
    rows: row 1 fully populated, row 2 with n_s = NULL (everything else
    matches row 1 so per-type WHERE clauses can disambiguate via id)."""
    table = f"{prefix}_marshal_check"
    ch_client.command(
        f"""
        CREATE TABLE `{table}` (
            id      Int32,
            s       String,
            fs      FixedString(8),
            u8      UInt8,
            i32     Int32,
            u64     UInt64,
            f64     Float64,
            b       Bool,
            d       Date,
            dt      DateTime,
            dt_tz   DateTime('UTC'),
            dt64_3  DateTime64(3),
            arr_s   Array(String),
            arr_i   Array(Int32),
            n_s     Nullable(String),
            arr_n_i Array(Nullable(Int32))
        ) ENGINE = Memory
        """
    )
    ch_client.command(
        f"""
        INSERT INTO `{table}` VALUES
            (1, 'hello', 'abcdefgh', 200, -7, 18446744073709551615, 3.14, true,
             '2026-05-09', '2026-05-09 12:00:00', '2026-05-09 12:00:00',
             '2026-05-09 12:34:56.789',
             ['alice','bob'], [1,2,3], 'filled', [1, NULL, 3]),
            (2, 'hello', 'abcdefgh', 200, -7, 18446744073709551615, 3.14, true,
             '2026-05-09', '2026-05-09 12:00:00', '2026-05-09 12:00:00',
             '2026-05-09 12:34:56.789',
             ['alice','bob'], [1,2,3], NULL, [1, NULL, 3])
        """
    )
    return table


# Each entry: (column, ch_type, python_value).
# WHERE col = {p:Type} AND id = {pid:Int32} should match exactly id=1.
_ROUNDTRIP_CASES = [
    ("s", "String", "hello"),
    ("fs", "FixedString(8)", "abcdefgh"),
    ("u8", "UInt8", 200),
    ("i32", "Int32", -7),
    ("u64", "UInt64", 18446744073709551615),
    ("f64", "Float64", 3.14),
    ("b", "Bool", True),
    ("d", "Date", date(2026, 5, 9)),
    ("dt", "DateTime", datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)),
    ("dt_tz", "DateTime('UTC')", datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC)),
    (
        "dt64_3",
        "DateTime64(3)",
        datetime(2026, 5, 9, 12, 34, 56, 789000, tzinfo=UTC),
    ),
    ("arr_s", "Array(String)", ["alice", "bob"]),
    ("arr_i", "Array(Int32)", [1, 2, 3]),
    ("n_s", "Nullable(String)", "filled"),
    ("arr_n_i", "Array(Nullable(Int32))", [1, None, 3]),
]


@pytest.mark.parametrize("col,ch_type,value", _ROUNDTRIP_CASES)
def test_marshal_roundtrip_returns_expected_row(
    http_client, marshal_table, col, ch_type, value
):
    """For each typed column: WHERE col = {p:Type} AND id = 1 returns row 1.

    Critically, dt64_3's value carries .789 milliseconds — the marshaller
    must preserve them or the WHERE clause won't match (this is the
    regression the type-driven dispatch fixes).
    """
    sql = (
        f"SELECT id FROM `{marshal_table}` "
        f"WHERE `{col}` = {{p:{ch_type}}} AND id = {{pid:Int32}}"
    )

    async def _run() -> list[dict[str, object]]:
        async with http_client() as client:
            return await _query_typed(
                client, sql=sql, parameters={"p": value, "pid": 1}
            )

    rows = asyncio.run(_run())
    assert rows == [{"id": 1}], (
        f"{col} ({ch_type}) roundtrip failed; rows={rows}"
    )


def test_nullable_string_null_row_matches_via_is_null(
    http_client, marshal_table
):
    """Row 2 has n_s = NULL. ``WHERE n_s = NULL`` is always false in SQL;
    use IS NULL. This validates that NULL values are stored correctly
    (and is the natural pair test for the populated-Nullable case)."""
    sql = f"SELECT id FROM `{marshal_table}` WHERE n_s IS NULL ORDER BY id"

    async def _run() -> list[dict[str, object]]:
        async with http_client() as client:
            return await _query_typed(client, sql=sql, parameters={})

    rows = asyncio.run(_run())
    assert rows == [{"id": 2}]


def test_array_of_nullable_with_null_element(
    http_client, marshal_table
):
    """Round-trip a list containing None inside Array(Nullable(Int32))."""
    sql = (
        f"SELECT id FROM `{marshal_table}` "
        f"WHERE arr_n_i = {{p:Array(Nullable(Int32))}} AND id = {{pid:Int32}}"
    )

    async def _run() -> list[dict[str, object]]:
        async with http_client() as client:
            return await _query_typed(
                client,
                sql=sql,
                parameters={"p": [1, None, 3], "pid": 1},
            )

    rows = asyncio.run(_run())
    assert rows == [{"id": 1}]


def test_marshal_array_with_bool_element_raises_before_http(
    http_client, marshal_table
):
    """``Array(Int32)`` with a bool element should raise TypeError from
    the marshaller before any HTTP call — bool is rejected by the int
    branch."""
    sql = (
        f"SELECT id FROM `{marshal_table}` "
        f"WHERE arr_i = {{p:Array(Int32)}} AND id = {{pid:Int32}}"
    )

    async def _run() -> None:
        async with http_client() as client:
            await _query_typed(
                client,
                sql=sql,
                parameters={"p": [1, True, 3], "pid": 1},
            )

    with pytest.raises(TypeError, match="rejects bool"):
        asyncio.run(_run())
