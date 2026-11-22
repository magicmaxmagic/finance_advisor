"""Nasdaq Trader symbol directory -> `universe_snapshot`, `security`.

**Start this on day one.** A point-in-time universe — which tickers were listed
on which date, including the ones that later stopped being listed — is the one
dataset that cannot be bought retroactively for free. Vendors sell you today's
constituents; the delisted names are simply gone. Every day this does not run is
a row that can never be recovered, and until the history accumulates, every
backtest that starts before capture began is survivorship-contaminated and has
to say so in `backtest_run.caveats`.

That is why snapshots are append-only and immutable. `write_daily_snapshot`
refuses to rewrite an existing day: a snapshot re-derived later from today's
listings is not a snapshot, it is a forgery of one.

Two files make up the directory — `nasdaqlisted.txt` and `otherlisted.txt` —
and each carries its own `File Creation Time` trailer. That trailer, not the
wall clock, is the snapshot date: running on a Sunday records Friday's file
under Friday's date rather than inventing a weekend that never traded.

This module also holds the fixed proof-of-concept universe: a hand-maintained
list of large-cap filers, pinned by CIK, used to exercise the pipeline on a
small sample.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import yaml

from finlab.config import Settings, get_settings
from finlab.ingest.sec import get_once
from finlab.ingest.warehouse import publish_table, write_daily_snapshot

POC_UNIVERSE_PATH = Path(__file__).resolve().parent.parent / "data" / "poc_universe.yaml"

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# "File Creation Time: 0731202621:31" -> 2026-07-31
_FILE_TIME = re.compile(r"File Creation Time:\s*(\d{2})(\d{2})(\d{4})(\d{2}):(\d{2})")

# NYSE and friends identify themselves by a single letter in otherlisted.txt.
_EXCHANGE_CODES = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
}

# Names that denote ordinary equity rather than a warrant, unit, right,
# preferred line or fund. Deliberately a whitelist: the failure mode we care
# about is a screen quietly including instruments that are not shares.
_COMMON_NAME = re.compile(
    r"(common stock|ordinary shares|common shares|class [a-z] (common|ordinary))",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Company:
    """One filer in the proof-of-concept universe."""

    cik: int
    ticker: str
    sector: str
    name: str
    stable_id: str | None = None

    @property
    def security_id(self) -> str:
        """The synthetic stable key from architecture §3.2.

        Built from the CIK, not the ticker, because tickers are recycled.
        """
        return self.stable_id or f"US:{self.cik:010d}:{self.ticker}"


class UniverseError(RuntimeError):
    """The universe file is missing or malformed."""


def load_poc_universe(path: Path | None = None) -> tuple[Company, ...]:
    """Load and validate the PoC universe, ordered by ticker."""
    target = path or get_settings().universe_path or POC_UNIVERSE_PATH
    return _load_universe_file(
        target.resolve(), target.stat().st_mtime_ns if target.exists() else 0
    )


@lru_cache(maxsize=8)
def _load_universe_file(target: Path, modified_ns: int) -> tuple[Company, ...]:
    if not target.is_file():
        raise UniverseError(f"PoC universe file not found: {target}")

    document = yaml.safe_load(target.read_text(encoding="utf-8"))
    entries = document.get("companies") if isinstance(document, dict) else None
    if not entries:
        raise UniverseError(f"{target} declares no companies")

    companies: list[Company] = []
    seen_cik: dict[int, str] = {}
    for entry in entries:
        missing = {"cik", "ticker", "sector", "name"} - set(entry)
        if missing:
            raise UniverseError(f"{target}: entry {entry!r} is missing {sorted(missing)}")
        cik = int(entry["cik"])
        if cik in seen_cik:
            raise UniverseError(
                f"{target}: CIK {cik} appears twice ({seen_cik[cik]} and {entry['ticker']})"
            )
        seen_cik[cik] = str(entry["ticker"])
        companies.append(
            Company(
                cik=cik,
                ticker=str(entry["ticker"]),
                sector=str(entry["sector"]),
                name=str(entry["name"]),
                stable_id=str(entry["security_id"]) if entry.get("security_id") else None,
            )
        )
    return tuple(sorted(companies, key=lambda c: c.ticker))


def sectors() -> tuple[str, ...]:
    return tuple(sorted({c.sector for c in load_poc_universe()}))


# ---------------------------------------------------------------------------
# the daily symbol directory
# ---------------------------------------------------------------------------


class SymbolDirectoryError(RuntimeError):
    """The symbol directory could not be parsed."""


@dataclass(slots=True)
class SnapshotReport:
    snapshot_date: date
    rows: int
    nasdaq: int
    other: int
    common: int
    cik_mapped: int
    path: Path | None = None
    skipped: bool = False
    raw_files: dict[str, str] = field(default_factory=dict)


def parse_file_creation_time(text: str) -> date:
    """Read the trailer that says when the exchange actually cut the file."""
    match = _FILE_TIME.search(text)
    if match is None:
        raise SymbolDirectoryError(
            "no 'File Creation Time' trailer — refusing to guess the snapshot date, "
            "because dating a snapshot wrongly is worse than not having it"
        )
    month, day, year, hour, minute = (int(g) for g in match.groups())
    return datetime(year, month, day, hour, minute).date()


def parse_symbol_directory(text: str, *, source: str) -> Iterator[dict[str, Any]]:
    """Yield one record per listed security from a pipe-delimited directory."""
    lines = text.splitlines()
    if not lines:
        raise SymbolDirectoryError(f"{source}: file is empty")

    header = [h.strip() for h in lines[0].split("|")]
    try:
        symbol_col = header.index("Symbol" if source == "nasdaq" else "ACT Symbol")
        name_col = header.index("Security Name")
        etf_col = header.index("ETF")
        test_col = header.index("Test Issue")
    except ValueError as exc:
        raise SymbolDirectoryError(f"{source}: unexpected header {header}") from exc
    exchange_col = header.index("Exchange") if "Exchange" in header else None

    for line in lines[1:]:
        if not line or line.startswith("File Creation Time"):
            continue
        parts = line.split("|")
        if len(parts) <= max(symbol_col, name_col, etf_col, test_col):
            continue
        ticker = parts[symbol_col].strip()
        if not ticker:
            continue
        name = parts[name_col].strip()
        is_etf = parts[etf_col].strip().upper() == "Y"
        is_test = parts[test_col].strip().upper() == "Y"
        if is_test:
            continue  # exchange test symbols are not tradable instruments
        exchange = (
            "NASDAQ"
            if exchange_col is None
            else _EXCHANGE_CODES.get(parts[exchange_col].strip(), parts[exchange_col].strip())
        )
        yield {
            "ticker": ticker,
            "name": name,
            "exchange": exchange,
            "is_common": bool(_COMMON_NAME.search(name)) and not is_etf,
        }


def _ticker_to_cik(payload: dict[str, Any]) -> dict[str, int]:
    return {
        str(row["ticker"]).upper(): int(row["cik_str"])
        for row in payload.values()
        if row.get("ticker")
    }


def snapshot_raw_dir(settings: Settings, snapshot_date: date) -> Path:
    return settings.raw_dir / "universe" / snapshot_date.isoformat()


def _store_raw(settings: Settings, snapshot_date: date, name: str, body: bytes) -> str:
    directory = snapshot_raw_dir(settings, snapshot_date)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{name}.gz"
    tmp = target.with_suffix(".gz.tmp")
    with gzip.open(tmp, "wb") as handle:
        handle.write(body)
    tmp.replace(target)
    return hashlib.sha256(body).hexdigest()


def capture_snapshot(
    con: duckdb.DuckDBPyConnection,
    settings: Settings | None = None,
    *,
    overwrite: bool = False,
) -> SnapshotReport:
    """Fetch today's symbol directory and record it as an immutable snapshot.

    Idempotent: re-running on a day already captured leaves the existing file
    alone and reports `skipped`, so a cron that fires twice, or a machine that
    was asleep over the weekend, cannot corrupt the record.
    """
    cfg = settings or get_settings()

    nasdaq_raw = get_once(NASDAQ_LISTED_URL, cfg)
    other_raw = get_once(OTHER_LISTED_URL, cfg)
    tickers_raw = get_once(COMPANY_TICKERS_URL, cfg)

    nasdaq_text = nasdaq_raw.decode("utf-8", errors="replace")
    other_text = other_raw.decode("utf-8", errors="replace")

    snapshot_date = parse_file_creation_time(nasdaq_text)
    other_date = parse_file_creation_time(other_text)
    if other_date != snapshot_date:
        # Mismatched cuts would put two different days in one snapshot.
        raise SymbolDirectoryError(
            f"the two directory files disagree on their creation date "
            f"({snapshot_date} vs {other_date}); refusing to merge them"
        )

    cik_by_ticker = _ticker_to_cik(json.loads(tickers_raw))

    records = [
        *parse_symbol_directory(nasdaq_text, source="nasdaq"),
        *parse_symbol_directory(other_text, source="other"),
    ]
    nasdaq_count = sum(1 for r in records if r["exchange"] == "NASDAQ")

    configured_ids = {
        company.ticker: (company.cik, company.security_id)
        for company in load_poc_universe(cfg.universe_path)
    }
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for record in records:
        source_ticker = record["ticker"].upper()
        ticker = source_ticker.replace(".", "-")
        if ticker in seen:
            continue  # a few symbols appear in both files
        seen.add(ticker)
        cik = cik_by_ticker.get(ticker, 0)
        rows.append(
            {
                "snapshot_date": snapshot_date,
                "source_ticker": source_ticker,
                "security_id": configured_ids[ticker][1]
                if ticker in configured_ids and configured_ids[ticker][0] == cik
                else f"US:{cik:010d}:{ticker}",
                "ticker": ticker,
                "cik": cik,
                "name": record["name"],
                "exchange": record["exchange"],
                "is_common": record["is_common"],
                # Needs a price and a share count; the price feed is unavailable
                # (see ingest/prices.py), so this stays NULL rather than wrong.
                "market_cap": None,
            }
        )

    frame = pl.DataFrame(
        rows,
        schema={
            "snapshot_date": pl.Date,
            "source_ticker": pl.Utf8,
            "security_id": pl.Utf8,
            "ticker": pl.Utf8,
            "cik": pl.Int32,
            "name": pl.Utf8,
            "exchange": pl.Utf8,
            "is_common": pl.Boolean,
            "market_cap": pl.Float64,
        },
    )
    con.register("_snapshot", frame)
    try:
        path, written = write_daily_snapshot(
            con,
            source="_snapshot",
            root=cfg.warehouse_dir / "universe_snapshot",
            snapshot_date=snapshot_date,
            order_by=("ticker",),
            overwrite=overwrite,
        )
    finally:
        con.unregister("_snapshot")

    raw_hashes = {}
    if written >= 0:
        raw_hashes = {
            "nasdaqlisted.txt": _store_raw(cfg, snapshot_date, "nasdaqlisted.txt", nasdaq_raw),
            "otherlisted.txt": _store_raw(cfg, snapshot_date, "otherlisted.txt", other_raw),
            "company_tickers.json": _store_raw(
                cfg, snapshot_date, "company_tickers.json", tickers_raw
            ),
        }

    return SnapshotReport(
        snapshot_date=snapshot_date,
        rows=len(rows),
        nasdaq=nasdaq_count,
        other=len(records) - nasdaq_count,
        common=sum(1 for r in rows if r["is_common"]),
        cik_mapped=sum(1 for r in rows if r["cik"]),
        path=path,
        skipped=written < 0,
        raw_files=raw_hashes,
    )


def rebuild_securities(con: duckdb.DuckDBPyConnection, settings: Settings | None = None) -> int:
    """Derive `security` and `ticker_history` from the accumulated snapshots.

    Recomputed rather than updated in place: the snapshots are the immutable
    record, so `first_date`, `last_date` and delisting are all functions of
    them. A security whose `last_date` stops advancing while later snapshots
    exist has been delisted — which is precisely the fact a survivorship-free
    backtest needs and no free vendor will sell you after the event.
    """
    cfg = settings or get_settings()
    root = cfg.warehouse_dir / "universe_snapshot"
    if not any(root.rglob("*.parquet")):
        return 0

    glob = str(root / "**" / "*.parquet")
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _snapshots AS
        SELECT * FROM read_parquet('{glob}', hive_partitioning = true, union_by_name = true)
        """
    )
    latest = con.execute("SELECT max(snapshot_date) FROM _snapshots").fetchone()[0]

    con.execute(
        """
        INSERT OR REPLACE INTO security
          (security_id, cik, ticker, exchange, share_class, currency,
           first_date, last_date, delisting_date, delisting_reason, is_active)
        SELECT security_id,
               nullif(any_value(cik), 0),
               any_value(ticker),
               last(exchange ORDER BY snapshot_date),
               NULL, 'USD',
               min(snapshot_date),
               max(snapshot_date),
               -- Absent from the most recent snapshot: no longer listed. The
               -- date is the last one it was seen, and the reason is unknown
               -- until a filing says otherwise.
               CASE WHEN max(snapshot_date) < ? THEN max(snapshot_date) END,
               NULL,
               max(snapshot_date) = ?
        FROM _snapshots
        GROUP BY security_id
        """,
        [latest, latest],
    )
    con.execute(
        """
        INSERT OR REPLACE INTO ticker_history (ticker, security_id, valid_from, valid_to)
        SELECT ticker, security_id, min(snapshot_date),
               CASE WHEN max(snapshot_date) = ? THEN DATE '9999-12-31'
                    ELSE max(snapshot_date) END
        FROM _snapshots
        GROUP BY ticker, security_id
        """,
        [latest],
    )
    publish_table(con, source="security", root=cfg.warehouse_dir / "security", order_by=("ticker",))
    return int(con.execute("SELECT count(*) FROM security").fetchone()[0])
