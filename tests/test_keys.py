"""The hashed surrogate keys for the warehouse tables."""

from __future__ import annotations

import duckdb
import pytest

from finlab.db.keys import FACT_KEY_FIELDS, PRICE_KEY_FIELDS, fact_key_sql, price_key_sql

_SOURCE = """
SELECT 320193 AS cik, 'us-gaap' AS taxonomy, 'Revenues' AS tag, 'USD' AS unit,
       {period_start} AS period_start, DATE '2024-09-28' AS period_end,
       '0000320193-24-000100' AS accession_number
"""


def _key(con: duckdb.DuckDBPyConnection, period_start: str) -> int:
    source = _SOURCE.format(period_start=period_start)
    return con.execute(f"SELECT {fact_key_sql()} FROM ({source})").fetchone()[0]


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


def test_key_is_stable_for_identical_input(con: duckdb.DuckDBPyConnection) -> None:
    assert _key(con, "DATE '2023-10-01'") == _key(con, "DATE '2023-10-01'")


def test_key_changes_when_any_field_changes(con: duckdb.DuckDBPyConnection) -> None:
    assert _key(con, "DATE '2023-10-01'") != _key(con, "DATE '2023-10-02'")


def test_null_period_start_does_not_collide_with_empty_string(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """concat_ws drops NULLs, so without a sentinel an instant fact would hash
    identically to a duration fact with an empty period_start."""
    assert _key(con, "NULL::DATE") != _key(con, "''")


def test_null_period_start_is_hashable_at_all(con: duckdb.DuckDBPyConnection) -> None:
    assert isinstance(_key(con, "NULL::DATE"), int)


def test_key_fits_in_a_signed_64_bit_column(con: duckdb.DuckDBPyConnection) -> None:
    """MD5's low 64 bits are unsigned; values above 2^63 must reinterpret as
    negative rather than overflow or clamp."""
    rows = con.execute(
        f"""
        SELECT {fact_key_sql()} FROM (
          SELECT i AS cik, 'us-gaap' AS taxonomy, 'T' AS tag, 'USD' AS unit,
                 NULL::DATE AS period_start, DATE '2024-01-01' AS period_end,
                 'acc' AS accession_number
          FROM range(2000) t(i)
        )
        """
    ).fetchall()
    keys = [r[0] for r in rows]
    assert len(set(keys)) == len(keys), "collision within 2000 rows"
    assert all(-(2**63) <= k < 2**63 for k in keys)
    assert any(k < 0 for k in keys), "no negative keys — reinterpretation may be clamping"


def test_price_key_covers_its_logical_key(con: duckdb.DuckDBPyConnection) -> None:
    first, second = con.execute(
        f"""
        SELECT {price_key_sql()} FROM (
          SELECT 'US:0000320193:AAPL' AS security_id, DATE '2024-01-02' AS date
          UNION ALL
          SELECT 'US:0000320193:AAPL', DATE '2024-01-03'
        )
        """
    ).fetchall()
    assert first[0] != second[0]


def test_field_lists_match_the_documented_logical_keys() -> None:
    assert FACT_KEY_FIELDS == (
        "cik",
        "taxonomy",
        "tag",
        "unit",
        "period_start",
        "period_end",
        "accession_number",
    )
    assert PRICE_KEY_FIELDS == ("security_id", "date")
