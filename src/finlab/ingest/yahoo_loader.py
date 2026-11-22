"""Load Yahoo normalized observations into FinLab's existing warehouse."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import polars as pl

from finlab.config import Settings
from finlab.db.conn import parquet_files
from finlab.db.keys import price_key_sql
from finlab.ingest.calendar import last_completed_session
from finlab.ingest.prices import build_securities
from finlab.ingest.providers import MarketDataProvider
from finlab.ingest.quality import PriceQualityError, validate_daily_prices
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
    files = parquet_files(root)
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
    columns = {
        r[0]
        for r in con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?, union_by_name=true)",
            [[str(path) for path in files]],
        ).fetchall()
    }
    optional = ", ".join(
        name if name in columns else f"NULL AS {name}"
        for name in ("adjusted_close", "adjustment_basis", "ingested_at")
    )
    rows = con.execute(
        "SELECT security_id, date, open, high, low, close, volume, source, "
        + optional
        + " FROM read_parquet(?, union_by_name=true)",
        [[str(path) for path in files]],
    ).to_arrow_table()
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
    if start > end:
        raise ValueError("start must be on or before end")
    selected = companies if companies is not None else load_poc_universe()
    securities = build_securities(con, settings, selected)

    tickers = [company.ticker for company in selected]
    downloaded = provider.get_daily_prices(tickers, start, end)
    if downloaded.is_empty():
        return YahooLoadReport(securities, 0, [], sorted(tickers), 0)

    # Fetch actions before advancing persisted price observations. An action
    # download failure must not move the next retry's recovery anchor forward.
    basis_date = last_completed_session(datetime.now(UTC))
    actions = provider.get_corporate_actions(tickers, start, max(end, basis_date))
    validate_daily_prices(downloaded, start, end)
    mapping = {company.ticker.upper(): company.security_id for company in selected}
    incoming = downloaded.filter(pl.col("symbol").is_in(sorted(mapping))).with_columns(
        pl.col("symbol").str.to_uppercase().replace_strict(mapping).alias("security_id")
    )
    incoming = incoming.with_columns(
        *[
            pl.lit(None).cast(pl.Float64 if name == "adjusted_close" else pl.Utf8).alias(name)
            for name in ("adjusted_close", "adjustment_basis")
            if name not in incoming.columns
        ],
        pl.lit(datetime.now(UTC)).alias("ingested_at"),
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
        "adjusted_close",
        "adjustment_basis",
        "ingested_at",
    )

    existing = _existing_price_frame(con, settings.warehouse_dir / "price_daily")
    # Vendor-adjusted histories can be revised after dividends or splits.
    # Mixing a newly rebased tail with the previous historical basis creates a
    # false return at the overlap boundary. Refresh only affected securities.
    for company in selected:
        old = existing.filter(pl.col("security_id") == company.security_id)
        new = incoming.filter(pl.col("security_id") == company.security_id)
        if old.is_empty() or new.is_empty():
            continue
        retained_dates = old.filter(pl.col("date").is_between(start, end)).select("date")
        if retained_dates.join(new.select("date"), on="date", how="anti").height:
            raise PriceQualityError(
                f"Provider omitted previously stored sessions for {company.ticker}"
            )
        if start <= old["date"].min():
            continue
        overlap = old.join(new, on=["security_id", "date"], suffix="_new")
        changed = overlap.filter((pl.col("close") - pl.col("close_new")).abs() > 1e-6)
        if "adjusted_close" in old.columns and "adjusted_close" in new.columns:
            changed_adjustment = overlap.filter(
                pl.col("adjusted_close").is_not_null()
                & pl.col("adjusted_close_new").is_not_null()
                & (
                    (
                        (pl.col("adjusted_close_new") / pl.col("close_new"))
                        / (pl.col("adjusted_close") / pl.col("close"))
                        - 1
                    ).abs()
                    > 1e-7
                )
            )
            basis_changed = not changed_adjustment.is_empty()
        else:
            basis_changed = False
        if not changed.is_empty() or basis_changed:
            load_yahoo_prices(
                con,
                settings,
                provider,
                (company,),
                start=old["date"].min(),
                end=max(end, old["date"].max()),
            )
            existing = _existing_price_frame(con, settings.warehouse_dir / "price_daily")
    # A second provider may use a different split/dividend convention. Do not
    # create a continuous-looking series by overwriting an incompatible history.
    for security_id in incoming["security_id"].unique().to_list():
        old_sources = set(existing.filter(pl.col("security_id") == security_id)["source"].to_list())
        new_sources = set(incoming.filter(pl.col("security_id") == security_id)["source"].to_list())
        if old_sources and old_sources != new_sources:
            raise PriceQualityError(f"Incompatible price providers for {security_id}")
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
    for action in actions:
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
            "(security_id, ex_date, action_type, factor, amount, source, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                security_id,
                ex_date,
                action_type,
                factor,
                amount,
                getattr(provider, "name", "unknown"),
                datetime.now(UTC),
            ],
        )
        action_count += 1

    for company in selected:
        if incoming.filter(pl.col("security_id") == company.security_id).is_empty():
            continue
        con.execute(
            """INSERT INTO price_ingestion VALUES (?,?,?,?,?,?)
            ON CONFLICT(security_id) DO UPDATE SET
            actions_checked_from=CASE WHEN price_ingestion.actions_checked_through>=excluded.actions_checked_from-1
              THEN least(price_ingestion.actions_checked_from,excluded.actions_checked_from) ELSE excluded.actions_checked_from END,
            actions_checked_through=excluded.actions_checked_through,price_basis_date=excluded.price_basis_date,
            ingested_at=excluded.ingested_at,source=excluded.source""",
            [
                company.security_id,
                getattr(provider, "name", "unknown"),
                start,
                max(end, basis_date),
                basis_date,
                datetime.now(UTC),
            ],
        )
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
