"""Incremental price recovery using persisted per-security observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import duckdb

from finlab.config import Settings
from finlab.db.conn import parquet_files
from finlab.ingest.providers import MarketDataProvider
from finlab.ingest.universe import Company, load_poc_universe
from finlab.ingest.yahoo_loader import YahooLoadReport, load_yahoo_prices


class IncompletePriceSync(RuntimeError):
    """The job must retry because one or more requested symbols were missing."""


@dataclass(frozen=True)
class PriceSyncWindow:
    start: date
    end: date
    companies: tuple[Company, ...]


def plan_price_sync(
    companies: tuple[Company, ...],
    latest: dict[str, date],
    *,
    history_start: date,
    end: date,
    overlap_days: int,
) -> tuple[PriceSyncWindow, ...]:
    """Group request windows; never advance a watermark from wall-clock time."""
    if history_start > end or overlap_days < 1:
        raise ValueError("history start must precede end and overlap must be positive")
    groups: dict[date, list[Company]] = {}
    for company in companies:
        observed = latest.get(company.security_id)
        # Ignore future observations when choosing the recovery anchor.
        start = (
            max(history_start, min(observed, end) - timedelta(days=overlap_days))
            if observed is not None
            else history_start
        )
        groups.setdefault(start, []).append(company)
    return tuple(
        PriceSyncWindow(start, end, tuple(sorted(group, key=lambda company: company.ticker)))
        for start, group in sorted(groups.items())
    )


def sync_prices(
    con: duckdb.DuckDBPyConnection,
    settings: Settings,
    provider: MarketDataProvider,
    *,
    end: date,
    companies: tuple[Company, ...] | None = None,
    full: bool = False,
) -> list[YahooLoadReport]:
    """Run under the existing writer lock. Partial downloads fail for queue retry.

    Successful observations are retained on failure; retry replans from persisted
    prices. This is not a publication boundary and never changes latest.json.
    """
    selected = (
        companies
        if companies is not None
        else (*load_poc_universe(), Company(884394, "SPY", "Benchmark", "SPDR S&P 500 ETF Trust"))
    )
    root = settings.warehouse_dir / "price_daily"
    latest = {}
    files = parquet_files(root)
    if files:
        latest = dict(
            con.execute(
                "SELECT security_id, max(date) FROM read_parquet(?, hive_partitioning=true) "
                "WHERE date <= ? GROUP BY security_id",
                [[str(path) for path in files], end],
            ).fetchall()
        )
    windows = plan_price_sync(
        selected,
        {} if full else latest,
        history_start=min(
            [
                settings.price_history_start,
                *[
                    row[0]
                    for row in con.execute(
                        "SELECT min(date) FROM read_parquet(?, hive_partitioning=true)",
                        [[str(path) for path in files]],
                    ).fetchall()
                    if row[0] is not None
                ],
            ]
        )
        if full and files
        else settings.price_history_start,
        end=end,
        overlap_days=settings.price_correction_overlap_days,
    )
    reports = []
    missing = []
    for window in windows:
        report = load_yahoo_prices(
            con, settings, provider, window.companies, start=window.start, end=window.end
        )
        reports.append(report)
        missing.extend(report.tickers_missing)
    if missing:
        raise IncompletePriceSync("No observations returned for: " + ", ".join(sorted(missing)))
    return reports
