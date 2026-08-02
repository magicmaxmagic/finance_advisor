"""Stooq bulk OHLCV -> `price_daily`. Raw prices only, never adj_close.

Adjustment factors are restated retroactively, so a vendor's `adj_close` is a
view of the split/dividend history *as known today*. Storing raw OHLCV and
deriving adjustments from `corporate_action` is what lets the adjusted series be
reconstructed as of any past date.

**Stooq is currently gated.** Both `stooq.com/db/d/` and the per-symbol CSV
endpoint now answer with a JavaScript proof-of-work challenge, and
`static.stooq.com` returns 401. `fetch_stooq_bulk` detects that and stops with
instructions rather than trying to defeat it: working around a site's anti-bot
control is not something this project does. The parser is therefore driven from
a local archive — download `d_us_txt.zip` by hand and point `--archive` at it —
which is also the shape M3's scheduled job will use once a reachable mirror is
settled on.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import httpx
import polars as pl

from finlab.config import Settings
from finlab.db.keys import price_key_sql
from finlab.ingest.universe import Company, load_poc_universe
from finlab.ingest.warehouse import publish_table, write_partitioned

STOOQ_BULK_URL = "https://stooq.com/db/d/?b=d_us_txt"

# The challenge page is small and always carries this marker.
_CHALLENGE_MARKER = "__verify"

_PRICE_SCHEMA = {
    "security_id": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Int64,
    "source": pl.Utf8,
}


class PriceSourceBlocked(RuntimeError):
    """The price source refused automated access."""


@dataclass(slots=True)
class PriceLoadReport:
    securities: int
    rows: int
    tickers_found: list[str]
    tickers_missing: list[str]


def fetch_stooq_bulk(settings: Settings, *, timeout: float = 60.0) -> bytes:
    """Attempt the bulk download. Raises `PriceSourceBlocked` when gated."""
    response = httpx.get(
        STOOQ_BULK_URL,
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": settings.sec_user_agent},
    )
    content_type = response.headers.get("content-type", "")
    if response.status_code == 401 or (
        "html" in content_type and _CHALLENGE_MARKER in response.text[:4000]
    ):
        raise PriceSourceBlocked(
            "Stooq is serving a JavaScript proof-of-work challenge instead of the "
            "bulk archive, and static.stooq.com returns 401. finlab will not work "
            "around a site's anti-bot control.\n"
            "Download d_us_txt.zip manually from https://stooq.com/db/h/ and run:\n"
            "  finlab ingest prices --archive /path/to/d_us_txt.zip"
        )
    response.raise_for_status()
    return response.content


def stooq_symbol(company: Company) -> str:
    """Stooq names US listings `ticker.us`, lower case, dots kept as dashes."""
    return f"{company.ticker.lower().replace('.', '-')}.us"


def _iter_archive_rows(
    archive: Path, wanted: dict[str, Company]
) -> Iterator[tuple[str, date, float, float, float, float, int, str]]:
    """Yield rows for the wanted symbols only, one archive member at a time.

    The full US archive is ~10k files; only the PoC universe is decoded, and
    never more than one member is held in memory.
    """
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".txt"):
                continue
            symbol = Path(info.filename).name.lower().removesuffix(".txt")
            company = wanted.get(symbol)
            if company is None:
                continue
            with bundle.open(info) as handle:
                text = io.TextIOWrapper(handle, encoding="utf-8", errors="replace")
                header = text.readline().strip().upper().split(",")
                try:
                    idx = {
                        name: header.index(name)
                        for name in ("DATE", "OPEN", "HIGH", "LOW", "CLOSE", "VOL")
                    }
                except ValueError:
                    continue
                for line in text:
                    parts = line.rstrip("\n").split(",")
                    if len(parts) <= max(idx.values()):
                        continue
                    try:
                        yield (
                            company.security_id,
                            date.fromisoformat(parts[idx["DATE"]]),
                            float(parts[idx["OPEN"]]),
                            float(parts[idx["HIGH"]]),
                            float(parts[idx["LOW"]]),
                            float(parts[idx["CLOSE"]]),
                            int(float(parts[idx["VOL"]] or 0)),
                            "stooq",
                        )
                    except ValueError:
                        continue


def build_securities(con: duckdb.DuckDBPyConnection, settings: Settings) -> int:
    """Populate `security` and `ticker_history` from the PoC universe.

    Needed independently of prices: the UI resolves a ticker to a CIK through
    this table, never by joining on the ticker string itself.
    """
    companies = load_poc_universe()
    frame = pl.DataFrame(
        {
            "security_id": [c.security_id for c in companies],
            "cik": [c.cik for c in companies],
            "ticker": [c.ticker for c in companies],
        },
        schema={"security_id": pl.Utf8, "cik": pl.Int32, "ticker": pl.Utf8},
    )
    con.register("_securities", frame)
    con.execute(
        """
        INSERT OR REPLACE INTO security
          (security_id, cik, ticker, exchange, share_class, currency,
           first_date, last_date, delisting_date, delisting_reason, is_active)
        SELECT security_id, cik, ticker, NULL, NULL, 'USD',
               NULL, NULL, NULL, NULL, TRUE
        FROM _securities
        """
    )
    con.execute(
        """
        INSERT OR REPLACE INTO ticker_history (ticker, security_id, valid_from, valid_to)
        SELECT ticker, security_id, DATE '1900-01-01', DATE '9999-12-31' FROM _securities
        """
    )
    con.unregister("_securities")
    publish_table(
        con, source="security", root=settings.warehouse_dir / "security", order_by=("ticker",)
    )
    return len(companies)


def load_stooq_archive(
    con: duckdb.DuckDBPyConnection, settings: Settings, archive: Path
) -> PriceLoadReport:
    """Parse a downloaded Stooq archive into the `price_daily` warehouse."""
    if not archive.is_file():
        raise FileNotFoundError(f"price archive not found: {archive}")

    companies = load_poc_universe()
    wanted = {stooq_symbol(c): c for c in companies}

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE price_stage (
          security_id VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE,
          close DOUBLE, volume BIGINT, source VARCHAR
        )
        """
    )
    chunk: list[tuple] = []
    total = 0

    def flush() -> None:
        nonlocal chunk, total
        if not chunk:
            return
        frame = pl.DataFrame(chunk, schema=_PRICE_SCHEMA, orient="row")
        con.register("_price_chunk", frame)
        con.execute("INSERT INTO price_stage SELECT * FROM _price_chunk")
        con.unregister("_price_chunk")
        total += len(chunk)
        chunk = []

    for row in _iter_archive_rows(archive, wanted):
        chunk.append(row)
        if len(chunk) >= 100_000:
            flush()
    flush()

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE price_publish AS
        SELECT {price_key_sql()} AS price_key, *, year(date) AS year
        FROM price_stage
        """
    )
    write_partitioned(
        con,
        source="price_publish",
        root=settings.warehouse_dir / "price_daily",
        partition_columns=("year",),
        order_by=("security_id", "date"),
    )

    found = {
        row[0] for row in con.execute("SELECT DISTINCT security_id FROM price_stage").fetchall()
    }
    return PriceLoadReport(
        securities=build_securities(con, settings),
        rows=total,
        tickers_found=sorted(c.ticker for c in companies if c.security_id in found),
        tickers_missing=sorted(c.ticker for c in companies if c.security_id not in found),
    )
