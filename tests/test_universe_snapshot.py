"""The daily universe snapshot — the one dataset that cannot be backfilled."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
import polars as pl
import pytest

from finlab.config import Settings
from finlab.ingest.universe import (
    SymbolDirectoryError,
    parse_file_creation_time,
    parse_symbol_directory,
    rebuild_securities,
)
from finlab.ingest.warehouse import write_daily_snapshot

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
File Creation Time: 0731202621:31|||||||
"""

OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
A|Agilent Technologies, Inc. Common Stock|N|A|N|100|N|A
AAA|Alternative Access First Priority CLO Bond ETF|P|AAA|Y|100|N|AAA
File Creation Time: 0731202621:31||||||
"""


def test_snapshot_date_comes_from_the_file_not_the_clock() -> None:
    """Running on a Sunday must record Friday's file under Friday's date rather
    than inventing a weekend that never traded."""
    assert parse_file_creation_time(NASDAQ) == date(2026, 7, 31)


def test_a_file_without_a_creation_time_is_refused() -> None:
    """Guessing the date would silently corrupt the one irreplaceable dataset."""
    with pytest.raises(SymbolDirectoryError, match="refusing to guess"):
        parse_file_creation_time("Symbol|Security Name\nAAPL|Apple Inc.\n")


def test_test_issues_are_excluded() -> None:
    tickers = {r["ticker"] for r in parse_symbol_directory(NASDAQ, source="nasdaq")}
    assert "ZXZZT" not in tickers, "exchange test symbols are not tradable instruments"
    assert tickers == {"AAPL", "QQQ"}


def test_etfs_are_recorded_but_not_marked_common() -> None:
    by_ticker = {r["ticker"]: r for r in parse_symbol_directory(NASDAQ, source="nasdaq")}
    assert by_ticker["AAPL"]["is_common"] is True
    assert by_ticker["QQQ"]["is_common"] is False


def test_exchange_codes_are_resolved() -> None:
    by_ticker = {r["ticker"]: r for r in parse_symbol_directory(OTHER, source="other")}
    assert by_ticker["A"]["exchange"] == "NYSE"
    assert by_ticker["AAA"]["exchange"] == "NYSE Arca"


def test_nasdaq_rows_carry_their_exchange() -> None:
    assert all(r["exchange"] == "NASDAQ" for r in parse_symbol_directory(NASDAQ, source="nasdaq"))


def test_an_unexpected_header_is_an_error() -> None:
    with pytest.raises(SymbolDirectoryError, match="unexpected header"):
        list(parse_symbol_directory("Nope|Nothing\nA|B\n", source="nasdaq"))


# --- the append-only writer -------------------------------------------------


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute(
        "CREATE TABLE snap AS SELECT * FROM (VALUES "
        "(DATE '2026-07-31', 'US:0000320193:AAPL', 'AAPL'), "
        "(DATE '2026-07-31', 'US:0000789019:MSFT', 'MSFT')"
        ") t(snapshot_date, security_id, ticker)"
    )
    return connection


def test_a_snapshot_is_written_under_its_year(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    path, rows = write_daily_snapshot(
        con,
        source="snap",
        root=tmp_path / "universe_snapshot",
        snapshot_date=date(2026, 7, 31),
        order_by=("ticker",),
    )
    assert rows == 2
    assert path == tmp_path / "universe_snapshot" / "year=2026" / "snapshot-2026-07-31.parquet"
    assert path.is_file()


def test_recapturing_a_day_leaves_it_untouched(
    con: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A snapshot re-derived later from today's listings is not a snapshot.

    A cron that fires twice, or a machine that wakes at the weekend, must not be
    able to overwrite history.
    """
    root = tmp_path / "universe_snapshot"
    kwargs = {"root": root, "snapshot_date": date(2026, 7, 31)}
    path, _ = write_daily_snapshot(con, source="snap", **kwargs)
    original = path.read_bytes()

    con.execute("DELETE FROM snap WHERE ticker = 'MSFT'")
    _, rows = write_daily_snapshot(con, source="snap", **kwargs)

    assert rows == -1, "an existing day must report as skipped"
    assert path.read_bytes() == original, "history was overwritten"


def test_overwrite_is_possible_but_explicit(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    root = tmp_path / "universe_snapshot"
    kwargs = {"root": root, "snapshot_date": date(2026, 7, 31)}
    write_daily_snapshot(con, source="snap", **kwargs)
    con.execute("DELETE FROM snap WHERE ticker = 'MSFT'")
    _, rows = write_daily_snapshot(con, source="snap", overwrite=True, **kwargs)
    assert rows == 1


def test_each_day_is_its_own_file(con: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    root = tmp_path / "universe_snapshot"
    write_daily_snapshot(con, source="snap", root=root, snapshot_date=date(2026, 7, 31))
    write_daily_snapshot(con, source="snap", root=root, snapshot_date=date(2026, 8, 3))
    assert len(list(root.rglob("*.parquet"))) == 2


# --- deriving securities from accumulated snapshots -------------------------


def _write_snapshot(settings: Settings, day: date, tickers: list[str]) -> None:
    con = duckdb.connect(":memory:")
    frame = pl.DataFrame(
        {
            "snapshot_date": [day] * len(tickers),
            "security_id": [f"US:0000000000:{t}" for t in tickers],
            "ticker": tickers,
            "cik": [0] * len(tickers),
            "name": [f"{t} Inc" for t in tickers],
            "exchange": ["NASDAQ"] * len(tickers),
            "is_common": [True] * len(tickers),
            "market_cap": [None] * len(tickers),
        },
        schema={
            "snapshot_date": pl.Date,
            "security_id": pl.Utf8,
            "ticker": pl.Utf8,
            "cik": pl.Int32,
            "name": pl.Utf8,
            "exchange": pl.Utf8,
            "is_common": pl.Boolean,
            "market_cap": pl.Float64,
        },
    )
    con.register("s", frame)
    write_daily_snapshot(
        con, source="s", root=settings.warehouse_dir / "universe_snapshot", snapshot_date=day
    )
    con.close()


def test_a_ticker_that_stops_appearing_is_marked_delisted(initialised: Settings) -> None:
    """This is the whole point: the delisted names are what no vendor sells you
    after the fact, and what survivorship-free backtesting needs."""
    from finlab.db.conn import write_conn

    _write_snapshot(initialised, date(2026, 7, 30), ["AAA", "BBB"])
    _write_snapshot(initialised, date(2026, 7, 31), ["AAA"])

    with write_conn(initialised) as con:
        assert rebuild_securities(con, initialised) == 2
        rows = {
            r[0]: r[1:]
            for r in con.execute(
                "SELECT ticker, first_date, last_date, delisting_date, is_active FROM security"
            ).fetchall()
        }

    assert rows["AAA"][3] is True
    assert rows["AAA"][2] is None, "still listed, so no delisting date"
    assert rows["BBB"][3] is False
    assert rows["BBB"][2] == date(2026, 7, 30), "delisted as of the last day it was seen"
    assert rows["BBB"][0] == date(2026, 7, 30)
