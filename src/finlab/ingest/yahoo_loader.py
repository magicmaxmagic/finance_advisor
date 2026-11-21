"""Load Yahoo normalized observations into FinLab's existing warehouse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import polars as pl

from finlab.config import Settings
from finlab.db.keys import price_key_sql
from finlab.ingest.prices import build_securities
from finlab.ingest.providers import MarketDataProvider
from finlab.ingest.universe import Company, load_poc_universe
from finlab.ingest.warehouse import write_partitioned


@dataclass(slots=True)
class YahooLoadReport:
    securities: int
    rows: int
    tickers_found: list[str]
    tickers_missing: list[str]
    corporate_actions: int


def _existing_price_frame(con: duckdb.DuckDBPyConnection, root: Path) -> pl.DataFrame:
    files = sorted(root.rglob("*.parquet"))
    if not files:
        return pl.DataFrame(
            schema={
                "security_id": pl.Utf8,
                "date": pl.Date,
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "volume": pl.Int64,
                "source": pl.Utf8,
            }
        )
    rows = con.execute(
        "SELECT security_id, date, open, high, low, close, volume, source "
        "FROM read_parquet(?, union_by_name=true)",
        [str(root / "**" / "*.parquet")],
    ).fetch_arrow_table()
    return pl.from_arrow(rows)


def load_yahoo_prices(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    provider: MarketDataProvider,
    companies: tuple[Company, ...] | None = None,
    *,
    start: date,
    end: date,
) -> YahooLoadReport:
    """Merge provider observations into `price_daily` under the caller's writer lock."""
    selected = companies or load_poc_universe()
    securities = build_securities(con, settings)

    tickers = [company.ticker for company in selected]
    downloaded = provider.get_daily_prices(tickers, start, end)
    if downloaded.is_empty():
        return YahooLoadReport(securities, 0, [], sorted(tickers), 0)

    mapping = {company.ticker.upper(): company.security_id for company in selected}
    incoming = downloaded.filter(pl.col("symbol").is_in(sorted(mapping))).with_columns(
        pl.col("symbol").str.to_uppercase().replace_strict(mapping).alias("security_id")
    )
    incoming = incoming.select(
        "security_id",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "source",
    )

    existing = _existing_price_frame(con, settings.warehouse_dir / "price_daily")
    existing = existing.with_columns(pl.lit(0).alias("_provider_priority"))
    incoming = incoming.with_columns(pl.lit(1).alias("_provider_priority"))
    merged = (
        pl.concat([existing, incoming], how="diagonal_relaxed")
        .sort(["security_id", "date", "_provider_priority"])
        .unique(subset=["security_id", "date"], keep="last", maintain_order=True)
        .drop("_provider_priority")
    )

    con.register("_price_stage", merged)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE price_publish AS "
        f"SELECT {price_key_sql()} AS price_key, *, year(date) AS year "
        f"FROM _price_stage"
    )

    write_partitioned(
        con,
        source="price_publish",
        root=settings.warehouse_dir / "price_daily",
        partition_columns=("year",),
        order_by=("security_id", "date"),
    )
    con.unregister("_price_stage")

    action_count = 0
    for action in provider.get_corporate_actions(tickers, start, end):
        symbol = str(action["symbol"]).upper()
        security_id = mapping.get(symbol)
        if security_id is None:
            continue
        action_type = str(action["action_type"])
        ex_date = action["ex_date"]
        factor = action.get("factor")
        amount = action.get("amount")
        con.execute(
            "INSERT OR REPLACE INTO corporate_action "
            "(security_id, ex_date, action_type, factor, amount) "
            "VALUES (?, ?, ?, ?, ?)",
            [security_id, ex_date, action_type, factor, amount],
        )
        action_count += 1

    found = set(incoming.select("security_id").to_series().to_list())
    found_tickers = sorted(company.ticker for company in selected if company.security_id in found)
    missing_tickers = sorted(
        company.ticker for company in selected if company.ticker not in found_tickers
    )
    return YahooLoadReport(
        securities=securities,
        rows=merged.height,
        tickers_found=found_tickers,
        tickers_missing=missing_tickers,
        corporate_actions=action_count,
    )
