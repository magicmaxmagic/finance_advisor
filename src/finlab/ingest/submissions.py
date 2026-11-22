"""EDGAR submissions -> `filing.accepted_at` and the entity descriptors.

companyfacts tells you what a filing *said* but not when it landed. That gap
matters more than it sounds: a 10-K accepted at 16:45 ET cannot inform a trade
at that day's close, so any signal built without `accepted_at` silently assumes
information it did not have. This module fills it in, along with
`period_of_report` and the primary document URL, plus the SIC code, fiscal year
end and state of incorporation that `entity` has been carrying as NULLs.

Submissions JSON acceptance strings are retained verbatim with an unverified
clock status. The legacy naive field remains for compatibility; it must not be
used as an intraday instant. Only SGML ACCEPTANCE-DATETIME verification populates
accepted_at_utc. Daily research uses filed dates and next-session execution.

"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import html
import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import polars as pl

from finlab.config import Settings, get_settings
from finlab.ingest.sec import SecClient, submissions_url
from finlab.ingest.universe import Company, load_poc_universe
from finlab.ingest.warehouse import publish_table

ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"


@dataclass(slots=True)
class SubmissionsReport:
    companies: int
    filings_seen: int
    filings_updated: int
    entities_updated: int
    missing: list[int] = field(default_factory=list)


def raw_dir(settings: Settings) -> Path:
    return settings.raw_dir / "submissions"


def raw_path(settings: Settings, cik: int) -> Path:
    return raw_dir(settings) / f"CIK{cik:010d}.json.gz"


def _as_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _as_timestamp(value: object) -> datetime | None:
    """Legacy wall-clock parsing only; this does not establish a verified instant."""
    if not isinstance(value, str) or not value:
        return None
    text = value.rstrip("Z")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def primary_doc_url(cik: int, accession: str, document: str) -> str | None:
    if not document:
        return None
    return ARCHIVE_URL.format(cik=cik, accession=accession.replace("-", ""), document=document)


def parse_submissions(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield one row per filing from a submissions document.

    Handles both the inline `filings.recent` block and the paged older files,
    which share a column-oriented layout: parallel arrays rather than records.
    """
    cik = int(payload["cik"]) if str(payload.get("cik", "")).strip() else 0
    blocks: list[dict[str, Any]] = []
    filings = payload.get("filings")
    if isinstance(filings, dict):
        if isinstance(filings.get("recent"), dict):
            blocks.append(filings["recent"])
    elif isinstance(payload.get("accessionNumber"), list):
        blocks.append(payload)  # a paged file is the bare block

    for block in blocks:
        accessions = block.get("accessionNumber") or []
        for i, accession in enumerate(accessions):

            def at(key: str, index: int = i, blk: dict[str, Any] = block) -> Any:
                column = blk.get(key) or []
                return column[index] if index < len(column) else None

            yield {
                "accession_number": accession,
                "cik": cik,
                "form": at("form"),
                "filed_date": _as_date(at("filingDate")),
                "accepted_at_raw": str(at("acceptanceDateTime"))
                if at("acceptanceDateTime") is not None
                else None,
                "accepted_at_status": "unverified_sec_json",
                "accepted_at": _as_timestamp(at("acceptanceDateTime")),
                "period_of_report": _as_date(at("reportDate")),
                "primary_doc_url": primary_doc_url(cik, accession, at("primaryDocument") or ""),
            }


def entity_descriptors(payload: dict[str, Any]) -> dict[str, Any]:
    sic = payload.get("sic")
    return {
        "cik": int(payload["cik"]),
        "sic": int(sic) if str(sic or "").isdigit() else None,
        "industry": payload.get("sicDescription") or None,
        "fiscal_year_end": payload.get("fiscalYearEnd") or None,
        "country_inc": payload.get("stateOfIncorporation") or None,
    }


def page_path(settings: Settings, name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("Unsafe SEC submissions page name")
    return raw_dir(settings) / f"{name}.gz"


def _write_gz(target: Path, body: bytes) -> None:
    blobs = target.parent / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    payloads = [body]
    if target.exists():
        with gzip.open(target, "rb") as handle:
            payloads.append(handle.read())
    for payload in payloads:
        blob = blobs / f"{hashlib.sha256(payload).hexdigest()}.json.gz"
        if not blob.exists():
            blob_tmp = blob.with_suffix(".tmp")
            blob_tmp.write_bytes(gzip.compress(payload, mtime=0))
            blob_tmp.replace(blob)
    tmp = target.with_suffix(".tmp")
    with gzip.open(tmp, "wb") as handle:
        handle.write(body)
    tmp.replace(target)


async def fetch_submissions(
    companies: Sequence[Company],
    settings: Settings,
    *,
    force: bool = False,
    refresh_archives: bool | None = None,
) -> list[int]:
    """Download the submissions documents for each company. Returns missing CIKs.

    `filings.recent` holds only the most recent 1,000 filings; anything older is
    paged into separate files listed under `filings.files`. Those pages are
    followed too, because a filer with a long history would otherwise be missing
    acceptance times for its early years — exactly the period a long backtest
    depends on.
    """
    raw_dir(settings).mkdir(parents=True, exist_ok=True)
    todo = [c for c in companies if force or not raw_path(settings, c.cik).is_file()]
    missing: list[int] = []

    async with SecClient(settings) as client:
        if todo:

            async def one(company: Company) -> None:
                body = await client.get_bytes(submissions_url(company.cik))
                if body is None:
                    missing.append(company.cik)
                    return
                _write_gz(raw_path(settings, company.cik), body)

            await asyncio.gather(*(one(c) for c in todo))

        # Second pass: the index files are on disk now, so the page list is known.
        pages: list[str] = []
        for company in companies:
            path = raw_path(settings, company.cik)
            if not path.is_file():
                continue
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
            for entry in (payload.get("filings") or {}).get("files") or []:
                name = entry.get("name")
                if name and (
                    (force if refresh_archives is None else refresh_archives)
                    or not page_path(settings, name).is_file()
                ):
                    pages.append(name)

        if pages:

            async def page(name: str) -> None:
                body = await client.get_bytes(f"{SUBMISSIONS_BASE}/{name}")
                if body is not None:
                    _write_gz(page_path(settings, name), body)

            await asyncio.gather(*(page(n) for n in pages))

    return missing


def load_submissions(
    con: duckdb.DuckDBPyConnection, companies: Sequence[Company], settings: Settings
) -> SubmissionsReport:
    """Update `filing` and `entity` from the cached submissions documents."""
    rows: list[dict[str, Any]] = []
    descriptors: list[dict[str, Any]] = []

    for company in companies:
        path = raw_path(settings, company.cik)
        if not path.is_file():
            continue
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        descriptors.append(entity_descriptors(payload))
        rows.extend(parse_submissions(payload))

        # Older filings live in paged files; they carry the bare column block
        # with no cik of their own, so it is supplied from the index.
        for entry in (payload.get("filings") or {}).get("files") or []:
            page = page_path(settings, entry.get("name") or "")
            if not page.is_file():
                continue
            with gzip.open(page, "rt", encoding="utf-8") as handle:
                block = json.load(handle)
            block["cik"] = company.cik
            rows.extend(parse_submissions(block))

    if not rows:
        return SubmissionsReport(companies=0, filings_seen=0, filings_updated=0, entities_updated=0)

    frame = pl.DataFrame(
        rows,
        schema={
            "accession_number": pl.Utf8,
            "cik": pl.Int32,
            "form": pl.Utf8,
            "filed_date": pl.Date,
            "accepted_at": pl.Datetime("us"),
            "accepted_at_raw": pl.Utf8,
            "accepted_at_status": pl.Utf8,
            "period_of_report": pl.Date,
            "primary_doc_url": pl.Utf8,
        },
    ).unique(subset=["accession_number"], keep="first")

    con.register("_submissions", frame)
    # Only filings we already know about are touched: companyfacts defines which
    # filings carry facts, and this fills in what it could not tell us.
    con.execute(
        """
        UPDATE filing SET
          accepted_at = s.accepted_at,
          accepted_at_raw = s.accepted_at_raw,
          accepted_at_status = CASE WHEN filing.accepted_at_utc IS NOT NULL THEN filing.accepted_at_status ELSE s.accepted_at_status END,
          period_of_report = coalesce(s.period_of_report, filing.period_of_report),
          primary_doc_url = s.primary_doc_url,
          form = coalesce(s.form, filing.form),
          is_amendment = coalesce(s.form, '') LIKE '%/A'
        FROM _submissions s
        WHERE s.accession_number = filing.accession_number
        """
    )
    updated = int(
        con.execute("SELECT count(*) FROM filing WHERE accepted_at IS NOT NULL").fetchone()[0]
    )
    con.unregister("_submissions")

    descriptor_frame = pl.DataFrame(
        descriptors,
        schema={
            "cik": pl.Int32,
            "sic": pl.Int32,
            "industry": pl.Utf8,
            "fiscal_year_end": pl.Utf8,
            "country_inc": pl.Utf8,
        },
    )
    con.register("_descriptors", descriptor_frame)
    con.execute(
        """
        UPDATE entity SET
          sic = d.sic,
          industry = d.industry,
          fiscal_year_end = d.fiscal_year_end,
          country_inc = d.country_inc,
          updated_at = now()::TIMESTAMP
        FROM _descriptors d
        WHERE d.cik = entity.cik
        """
    )
    entities = int(con.execute("SELECT count(*) FROM entity WHERE sic IS NOT NULL").fetchone()[0])
    con.unregister("_descriptors")

    publish_table(con, source="entity", root=settings.warehouse_dir / "entity", order_by=("cik",))
    publish_table(
        con,
        source="filing",
        root=settings.warehouse_dir / "filing",
        order_by=("cik", "filed_date"),
    )

    return SubmissionsReport(
        companies=len(descriptors),
        filings_seen=len(frame),
        filings_updated=updated,
        entities_updated=entities,
    )


def ingest_submissions(
    con: duckdb.DuckDBPyConnection,
    settings: Settings | None = None,
    *,
    force: bool = False,
    refresh_archives: bool | None = None,
) -> SubmissionsReport:
    cfg = settings or get_settings()
    companies = load_poc_universe()
    missing = asyncio.run(
        fetch_submissions(companies, cfg, force=force, refresh_archives=refresh_archives)
    )
    report = load_submissions(con, companies, cfg)
    report.missing = missing
    return report


def verify_acceptance_sgml(
    con: duckdb.DuckDBPyConnection, accession: str, sgml_text: str
) -> datetime:
    """Verify the acceptance clock from the SEC SGML header (New York time)."""
    header = html.unescape(sgml_text)
    identity = re.search(r"<ACCESSION-NUMBER>\s*([0-9-]+)", header)
    if identity is None or identity.group(1) != accession:
        raise ValueError("SGML accession identity does not match the requested filing")
    match = re.search(r"<ACCEPTANCE-DATETIME>\s*(\d{14})", html.unescape(sgml_text))
    if match is None:
        raise ValueError("No SGML acceptance timestamp found")
    accepted = (
        datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        .replace(tzinfo=ZoneInfo("America/New_York"))
        .astimezone(UTC)
    )
    con.execute(
        "UPDATE filing SET accepted_at_utc=?,accepted_at_status='verified_sgml' WHERE accession_number=?",
        [accepted, accession],
    )
    return accepted
