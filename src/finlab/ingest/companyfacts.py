"""SEC companyfacts -> the `fact` warehouse, plus `entity` and `filing`.

Two layers, deliberately separated:

- **Parsing and writing** (`parse_companyfacts`, `load_parsed`) know nothing
  about where the JSON came from. They take a decoded companyfacts document and
  produce rows. M1's bulk loader streams the same documents out of
  companyfacts.zip and reuses both unchanged.
- **Fetching** (`fetch_poc_universe`) is the proof-of-concept source: the
  per-company endpoint, ~50 filers, minutes rather than hours.

Raw responses are stored gzipped and hashed under `data/raw/`, and a manifest
records which CIKs completed. A resumed run re-parses from those raw files
rather than re-downloading, which is what makes rewriting whole partitions
correct: the warehouse is always derived from the complete set of raw documents
on disk, never appended to incrementally.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import resource
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from finlab.config import Settings, get_settings
from finlab.db.keys import fact_key_sql
from finlab.ingest.prices import build_securities
from finlab.ingest.sec import SecClient, companyfacts_url
from finlab.ingest.universe import Company, load_poc_universe
from finlab.ingest.warehouse import WriteResult, publish_table, write_partitioned

# Column order used by the staging table, the polars frames and the parquet
# files alike. fact_key is derived in SQL and is not part of this tuple.
FACT_COLUMNS: tuple[str, ...] = (
    "cik",
    "taxonomy",
    "tag",
    "unit",
    "period_start",
    "period_end",
    "duration_days",
    "duration_bucket",
    "value",
    "accession_number",
    "form",
    "fiscal_year",
    "fiscal_period",
    "filed_date",
    "frame",
)

_FACT_SCHEMA: dict[str, Any] = {
    "cik": pl.Int32,
    "taxonomy": pl.Utf8,
    "tag": pl.Utf8,
    "unit": pl.Utf8,
    "period_start": pl.Date,
    "period_end": pl.Date,
    "duration_days": pl.Int32,
    "duration_bucket": pl.Utf8,
    "value": pl.Float64,
    "accession_number": pl.Utf8,
    "form": pl.Utf8,
    "fiscal_year": pl.Int32,
    "fiscal_period": pl.Utf8,
    "filed_date": pl.Date,
    "frame": pl.Utf8,
}

# A parser bug that silently changed values would be invisible in aggregate, so
# conflicting duplicates are counted and the run fails past this fraction.
CONFLICT_TOLERANCE = 0.001

CHUNK_ROWS = 50_000


class IngestError(RuntimeError):
    """Ingestion produced a result we refuse to publish."""


# ---------------------------------------------------------------------------
# parsing — pure, source-agnostic, reused by the M1 bulk loader
# ---------------------------------------------------------------------------


def duration_bucket(duration_days: int | None) -> str:
    """Classify a reporting window (architecture §3.4).

    Without this a 10-Q's three-month and nine-month `Revenues` rows look
    identical and get summed together.
    """
    if duration_days is None:
        return "instant"
    if 80 <= duration_days <= 100:
        return "quarter"
    if 170 <= duration_days <= 190:
        return "half"
    if 260 <= duration_days <= 285:
        return "ytd9"
    if 350 <= duration_days <= 380:
        return "annual"
    return "other"


@dataclass(slots=True)
class ParseStats:
    observations: int = 0
    emitted: int = 0
    skipped_missing_field: int = 0
    skipped_null_value: int = 0
    skipped_bad_date: int = 0


def _as_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_companyfacts(
    payload: Mapping[str, Any], stats: ParseStats | None = None
) -> Iterator[tuple]:
    """Yield one row per reported observation, in FACT_COLUMNS order.

    Every observation is emitted, including the same period reported by several
    filings — that redundancy is the restatement history and is the entire
    reason this table exists.
    """
    counters = stats if stats is not None else ParseStats()
    cik = int(payload["cik"])

    for taxonomy, tags in (payload.get("facts") or {}).items():
        for tag, body in (tags or {}).items():
            for unit, observations in ((body or {}).get("units") or {}).items():
                for obs in observations or ():
                    counters.observations += 1

                    value = obs.get("val")
                    accession = obs.get("accn")
                    if accession is None or "end" not in obs or "filed" not in obs:
                        counters.skipped_missing_field += 1
                        continue
                    if value is None:
                        counters.skipped_null_value += 1
                        continue

                    period_end = _as_date(obs.get("end"))
                    filed_date = _as_date(obs.get("filed"))
                    if period_end is None or filed_date is None:
                        counters.skipped_bad_date += 1
                        continue

                    period_start = _as_date(obs.get("start"))
                    days = (period_end - period_start).days if period_start else None

                    counters.emitted += 1
                    yield (
                        cik,
                        taxonomy,
                        tag,
                        unit,
                        period_start,
                        period_end,
                        days,
                        duration_bucket(days),
                        float(value),
                        accession,
                        obs.get("form"),
                        obs.get("fy"),
                        obs.get("fp"),
                        filed_date,
                        obs.get("frame"),
                    )


# ---------------------------------------------------------------------------
# raw storage and the resume manifest
# ---------------------------------------------------------------------------


def raw_dir(settings: Settings) -> Path:
    return settings.raw_dir / "companyfacts" / "poc"


def raw_path(settings: Settings, cik: int) -> Path:
    return raw_dir(settings) / f"CIK{cik:010d}.json.gz"


def manifest_path(settings: Settings) -> Path:
    return raw_dir(settings) / "manifest.json"


def load_manifest(settings: Settings) -> dict[str, dict[str, Any]]:
    path = manifest_path(settings)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_manifest(settings: Settings, manifest: Mapping[str, dict[str, Any]]) -> None:
    path = manifest_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(sorted(manifest.items())), indent=1), encoding="utf-8")
    tmp.replace(path)


def read_raw(settings: Settings, cik: int) -> dict[str, Any]:
    with gzip.open(raw_path(settings, cik), "rt", encoding="utf-8") as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# fetching — the proof-of-concept source
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FetchReport:
    downloaded: list[int] = field(default_factory=list)
    cached: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)


async def fetch_poc_universe(
    companies: Sequence[Company], settings: Settings, *, force: bool = False
) -> FetchReport:
    """Download companyfacts for each CIK, skipping ones already on disk."""
    report = FetchReport()
    manifest = load_manifest(settings)
    raw_dir(settings).mkdir(parents=True, exist_ok=True)

    todo: list[Company] = []
    for company in companies:
        if not force and raw_path(settings, company.cik).is_file():
            report.cached.append(company.cik)
            continue
        todo.append(company)

    if not todo:
        return report

    async with SecClient(settings) as client:

        async def one(company: Company) -> None:
            body = await client.get_bytes(companyfacts_url(company.cik))
            if body is None:
                report.missing.append(company.cik)
                return
            target = raw_path(settings, company.cik)
            tmp = target.with_suffix(".tmp")
            with gzip.open(tmp, "wb") as handle:
                handle.write(body)
            tmp.replace(target)
            manifest[str(company.cik)] = {
                "ticker": company.ticker,
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            report.downloaded.append(company.cik)

        await asyncio.gather(*(one(c) for c in todo))

    save_manifest(settings, manifest)
    return report


# ---------------------------------------------------------------------------
# loading — staging, conflict detection, partition write
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LoadReport:
    parse: ParseStats
    staged_rows: int
    distinct_keys: int
    conflicting_keys: int
    conflicting_rows: int
    entities: int
    filings: int
    write: WriteResult | None = None

    @property
    def conflict_fraction(self) -> float:
        return self.conflicting_rows / self.staged_rows if self.staged_rows else 0.0


_STAGE_DDL = """
CREATE OR REPLACE TEMP TABLE fact_stage (
  cik INTEGER, taxonomy VARCHAR, tag VARCHAR, unit VARCHAR,
  period_start DATE, period_end DATE, duration_days INTEGER,
  duration_bucket VARCHAR, value DOUBLE, accession_number VARCHAR,
  form VARCHAR, fiscal_year INTEGER, fiscal_period VARCHAR,
  filed_date DATE, frame VARCHAR
)
"""


def _stage_rows(con: duckdb.DuckDBPyConnection, rows: Iterator[tuple]) -> int:
    """Insert parsed rows in bounded chunks.

    Never materialises the whole parse into Python memory: at most CHUNK_ROWS
    tuples exist at once, handed to DuckDB through Arrow via polars.
    """
    con.execute(_STAGE_DDL)
    total = 0
    chunk: list[tuple] = []

    def flush() -> None:
        nonlocal total, chunk
        if not chunk:
            return
        frame = pl.DataFrame(chunk, schema=_FACT_SCHEMA, orient="row")
        con.register("_chunk", frame)
        con.execute(f"INSERT INTO fact_stage SELECT {', '.join(FACT_COLUMNS)} FROM _chunk")
        con.unregister("_chunk")
        total += len(chunk)
        chunk = []

    for row in rows:
        chunk.append(row)
        if len(chunk) >= CHUNK_ROWS:
            flush()
    flush()
    return total


def load_parsed(
    con: duckdb.DuckDBPyConnection,
    rows: Iterator[tuple],
    companies: Sequence[Company],
    settings: Settings,
    entity_names: Mapping[int, str],
) -> LoadReport:
    """Stage rows, verify them, then publish facts, entities and filings."""
    staged = _stage_rows(con, rows)

    con.execute(
        f"CREATE OR REPLACE TEMP TABLE fact_keyed AS "
        f"SELECT {fact_key_sql()} AS fact_key, * FROM fact_stage"
    )

    distinct_keys, conflicting_keys, conflicting_rows = con.execute(
        """
        WITH grouped AS (
          SELECT fact_key, count(*) AS n, count(DISTINCT value) AS distinct_values
          FROM fact_keyed GROUP BY fact_key
        )
        SELECT count(*),
               coalesce(sum(CASE WHEN distinct_values > 1 THEN 1 ELSE 0 END), 0),
               coalesce(sum(CASE WHEN distinct_values > 1 THEN n ELSE 0 END), 0)
        FROM grouped
        """
    ).fetchone()

    report = LoadReport(
        parse=ParseStats(),
        staged_rows=staged,
        distinct_keys=int(distinct_keys),
        conflicting_keys=int(conflicting_keys),
        conflicting_rows=int(conflicting_rows),
        entities=0,
        filings=0,
    )

    if report.conflict_fraction > CONFLICT_TOLERANCE:
        raise IngestError(
            f"{report.conflicting_rows} of {staged} rows ({report.conflict_fraction:.4%}) "
            f"share a fact_key but disagree on value, over the "
            f"{CONFLICT_TOLERANCE:.1%} tolerance. Identical logical keys with "
            f"different values means the parser is wrong, not that a restatement "
            f"was found — restatements differ by accession_number, which is part "
            f"of the key."
        )

    # One row per logical key. Which duplicate survives is irrelevant: they are
    # identical in value, or we would have failed above.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE fact_final AS
        SELECT * EXCLUDE (rn) FROM (
          SELECT *, row_number() OVER (PARTITION BY fact_key ORDER BY value) AS rn
          FROM fact_keyed
        ) WHERE rn = 1
        """
    )

    con.execute(
        "CREATE OR REPLACE TEMP TABLE fact_publish AS "
        "SELECT *, year(filed_date) AS filed_year FROM fact_final"
    )

    report.write = write_partitioned(
        con,
        source="fact_publish",
        root=settings.warehouse_dir / "fact",
        partition_columns=("taxonomy", "filed_year"),
        order_by=("cik", "tag", "period_end"),
    )

    report.entities = _load_entities(con, companies, entity_names)
    report.filings = _load_filings(con)

    # Securities are derived from the universe file, not from prices, so the
    # ticker -> CIK resolution the UI needs exists even when the price source is
    # unavailable.
    build_securities(con, settings)

    # Readers may not open the DuckDB file, so the small tables they need are
    # snapshotted to the warehouse alongside the partitioned datasets.
    publish_table(con, source="entity", root=settings.warehouse_dir / "entity", order_by=("cik",))
    publish_table(
        con,
        source="filing",
        root=settings.warehouse_dir / "filing",
        order_by=("cik", "filed_date"),
    )
    return report


def _load_entities(
    con: duckdb.DuckDBPyConnection,
    companies: Sequence[Company],
    entity_names: Mapping[int, str],
) -> int:
    """Populate `entity`.

    companyfacts carries the filer's name but no SIC code, so `sector` comes
    from the PoC universe file and `sic`, `industry`, `country_inc` and
    `fiscal_year_end` stay NULL until the submissions endpoint is ingested.
    """
    frame = pl.DataFrame(
        {
            "cik": [c.cik for c in companies],
            "name": [entity_names.get(c.cik, c.name) for c in companies],
            "sector": [c.sector for c in companies],
        },
        schema={"cik": pl.Int32, "name": pl.Utf8, "sector": pl.Utf8},
    )
    con.register("_entities", frame)
    con.execute(
        """
        INSERT OR REPLACE INTO entity
          (cik, name, sic, sector, industry, country_inc, fiscal_year_end,
           first_filing, last_filing, is_active, updated_at)
        SELECT e.cik, e.name, NULL, e.sector, NULL, NULL, NULL,
               f.first_filing, f.last_filing, TRUE, now()::TIMESTAMP
        FROM _entities e
        LEFT JOIN (
          SELECT cik, min(filed_date) AS first_filing, max(filed_date) AS last_filing
          FROM fact_final GROUP BY cik
        ) f USING (cik)
        """
    )
    con.unregister("_entities")
    return int(con.execute("SELECT count(*) FROM entity").fetchone()[0])


def _load_filings(con: duckdb.DuckDBPyConnection) -> int:
    """Populate `filing` from the facts themselves.

    companyfacts exposes each observation's accession, form, fiscal year/period
    and filed date, but not `accepted_at` (intraday) or the declared
    `period_of_report`. accepted_at stays NULL — the EDGAR submissions index is
    where it comes from — and period_of_report is approximated by the latest
    period any fact in the filing covers. Both are recorded as gaps rather than
    guessed at silently, because same-day signal timing depends on accepted_at.
    """
    con.execute(
        """
        INSERT OR REPLACE INTO filing
          (accession_number, cik, form, filed_date, accepted_at, period_of_report,
           fiscal_year, fiscal_period, is_amendment, amends_accession,
           primary_doc_url, raw_path, raw_sha256)
        SELECT accession_number,
               any_value(cik),
               coalesce(mode(form), 'unknown'),
               min(filed_date),
               NULL,
               max(period_end),
               mode(fiscal_year),
               mode(fiscal_period),
               coalesce(mode(form), '') LIKE '%/A',
               NULL, NULL, NULL, NULL
        FROM fact_final
        GROUP BY accession_number
        """
    )
    return int(con.execute("SELECT count(*) FROM filing").fetchone()[0])


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IngestReport:
    fetch: FetchReport
    load: LoadReport
    peak_rss_mb: float


def parse_all(
    settings: Settings, companies: Sequence[Company]
) -> tuple[Iterator[tuple], ParseStats, dict[int, str]]:
    """A single lazy stream over every cached raw document."""
    stats = ParseStats()
    names: dict[int, str] = {}

    def stream() -> Iterator[tuple]:
        for company in companies:
            if not raw_path(settings, company.cik).is_file():
                continue
            payload = read_raw(settings, company.cik)
            names[company.cik] = str(payload.get("entityName") or company.name)
            yield from parse_companyfacts(payload, stats)

    return stream(), stats, names


def peak_rss_mb() -> float:
    """Peak resident set size of this process, in MiB."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def ingest_poc(
    con: duckdb.DuckDBPyConnection,
    settings: Settings | None = None,
    *,
    force: bool = False,
) -> IngestReport:
    """Fetch (if needed), parse, verify and publish the PoC universe."""
    cfg = settings or get_settings()
    companies = load_poc_universe()

    fetch_report = asyncio.run(fetch_poc_universe(companies, cfg, force=force))

    rows, stats, names = parse_all(cfg, companies)
    load_report = load_parsed(con, rows, companies, cfg, names)
    load_report.parse = stats

    return IngestReport(fetch=fetch_report, load=load_report, peak_rss_mb=peak_rss_mb())
