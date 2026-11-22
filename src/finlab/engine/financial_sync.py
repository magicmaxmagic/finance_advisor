"""Incremental statement discovery with a weekly full correction backstop."""

import asyncio
import gzip
import json
from datetime import UTC, datetime, timedelta

from finlab.ingest.companyfacts import (
    FACT_COLUMNS,
    fetch_poc_universe,
    ingest_poc,
    load_manifest,
    parse_companyfacts,
    read_raw,
    save_manifest,
)
from finlab.ingest.submissions import (
    ingest_submissions,
    load_submissions,
    parse_submissions,
    raw_path,
)
from finlab.ingest.universe import load_poc_universe


def sync_financials(con, settings, *, full: bool = False) -> int:
    """Publish nothing here. Caller publishes only after its entire job succeeds."""
    companies = load_poc_universe(settings.universe_path)
    if full:
        report = ingest_poc(con, settings, force=True)
        if report.fetch.missing:
            raise RuntimeError(f"Missing SEC companyfacts: {report.fetch.missing}")
    submissions = ingest_submissions(con, settings, force=True, refresh_archives=full)
    if submissions.missing:
        raise RuntimeError(f"Missing SEC submissions: {submissions.missing}")
    manifest = load_manifest(settings)
    forms = {
        "10-K",
        "10-K/A",
        "10-Q",
        "10-Q/A",
        "10-KT",
        "10-QT",
        "20-F",
        "20-F/A",
        "40-F",
        "40-F/A",
    }
    cutoff = datetime.now(UTC).date() - timedelta(days=30)
    recent = {}
    affected = []
    for company in companies:
        with gzip.open(raw_path(settings, company.cik), "rt") as handle:
            document = json.load(handle)
        recent[company.cik] = {
            row["accession_number"]: row["form"]
            for row in parse_submissions(document)
            if row["form"] in forms
            and row["filed_date"] is not None
            and row["filed_date"] >= cutoff
        }
        checked = set(manifest.get(str(company.cik), {}).get("checked_statement_accessions", []))
        if full or set(recent[company.cik]) - checked:
            affected.append(company)
    if affected and not full:
        fetched = asyncio.run(fetch_poc_universe(affected, settings, force=True))
        if fetched.missing:
            raise RuntimeError(f"Missing new SEC statement data: {fetched.missing}")
        report = ingest_poc(con, settings, force=False)
        if report.fetch.missing:
            raise RuntimeError(f"Missing SEC companyfacts: {report.fetch.missing}")
        load_submissions(con, companies, settings)
    manifest = load_manifest(settings)
    pending = []
    accession_index = FACT_COLUMNS.index("accession_number")
    for company in affected:
        available = {
            row[accession_index] for row in parse_companyfacts(read_raw(settings, company.cik))
        }
        checked = set(manifest[str(company.cik)].get("checked_statement_accessions", []))
        for accession, form in recent[company.cik].items():
            # Amendments without XBRL can be narrative-only. Original financial
            # filings must appear in companyfacts before the cycle is complete.
            if accession in available or form.endswith("/A"):
                checked.add(accession)
            else:
                pending.append(accession)
        manifest[str(company.cik)]["checked_statement_accessions"] = sorted(checked)
        manifest[str(company.cik)]["statements_checked_at"] = datetime.now(UTC).isoformat()
    save_manifest(settings, manifest)
    if pending:
        raise RuntimeError(
            f"SEC companyfacts is pending for {len(pending)} newly filed statements; retry required"
        )
    return len(affected)
