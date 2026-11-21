"""Fetch current inputs, apply the existing PIT macro, and archive each published brief."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import os
import zipfile
from collections import defaultdict
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import httpx

from finlab.ingest.weekly_prices import COLUMNS, download, read_csv
from finlab.research.weekly import BENCHMARK, MODEL, Bars, Filing, completed_friday, screen

if TYPE_CHECKING:
    from finlab.config import Settings


def _annual_facts(settings: Settings, companies: tuple, week: date) -> tuple[dict, dict]:
    from finlab.ingest.companyfacts import fetch_poc_universe

    # A failed refresh never silently reuses old files.
    fetched = asyncio.run(fetch_poc_universe(companies, settings, force=True))
    return resolve_annual_facts(settings, companies, week, set(fetched.downloaded))


def resolve_annual_facts(
    settings: Settings, companies: tuple, week: date, available_ciks: set[int]
) -> tuple[dict, dict]:
    """Resolve downloaded annual observations through the shared PIT macro."""
    import polars as pl

    from finlab.core.concepts import load_concept_map_file
    from finlab.core.pit import install_macros
    from finlab.db.conn import read_conn
    from finlab.ingest.companyfacts import (
        FACT_COLUMNS,
        load_manifest,
        parse_companyfacts,
        read_raw,
    )

    manifest = load_manifest(settings)
    mappings = [m for m in load_concept_map_file() if m.canonical == "revenue"]
    eps_tag = "EarningsPerShareDiluted"
    tags = {m.tag: "USD" for m in mappings} | {eps_tag: "USD/shares"}
    records = []
    hashes = {}
    for company in companies:
        if company.cik not in available_ciks:
            continue
        doc = read_raw(settings, company.cik)
        if int(doc["cik"]) != company.cik:
            raise ValueError(f"SEC identity mismatch for {company.ticker}.")
        hashes[company.ticker] = manifest[str(company.cik)]["sha256"]
        gaap = doc.get("facts", {}).get("us-gaap", {})
        selected = {
            tag: {"units": {unit: gaap[tag]["units"].get(unit, [])}}
            for tag, unit in tags.items()
            if tag in gaap
        }
        for record in parse_companyfacts({"cik": company.cik, "facts": {"us-gaap": selected}}):
            values = dict(zip(FACT_COLUMNS, record, strict=True))
            if (
                values["duration_bucket"] == "annual"
                and values["form"] in ("10-K", "10-K/A", "10-Q", "10-Q/A")
                and week - timedelta(days=850) <= values["period_end"] <= week
                and values["filed_date"] <= week
            ):
                records.append(values)
    if not records:
        raise ValueError("No eligible annual SEC facts could be refreshed.")
    # These small in-memory views keep the weekly refresh independent of full
    # warehouse rebuilds. The same fundamentals_asof macro resolves filing vintages.
    mapping = [
        (m.canonical, m.taxonomy, m.tag, m.priority, m.sign, m.valid_from, m.valid_to)
        for m in mappings
    ]
    mapping.append(("diluted_eps", "us-gaap", eps_tag, 1, 1, date(1900, 1, 1), date.max))
    with read_conn(settings, attach_warehouse=False) as con:
        # Conflicting observations at identical tag, period and filing vintage
        # have no defensible winner. Preserve their position in the PIT selection
        # with an unknown value, so a conflicting latest FY cannot use an older one.
        keys = [
            "cik",
            "taxonomy",
            "tag",
            "period_end",
            "duration_bucket",
            "filed_date",
            "accession_number",
        ]
        normalized = (
            pl.DataFrame(records, infer_schema_length=None)
            .group_by(keys)
            .agg(
                pl.col("period_start").min(),
                pl.when(pl.col("value").n_unique() == 1)
                .then(pl.col("value").first())
                .otherwise(float("nan"))
                .alias("value"),
            )
        )
        con.register("fact", normalized)
        con.register(
            "concept_map",
            pl.DataFrame(
                mapping,
                orient="row",
                schema=[
                    "canonical",
                    "taxonomy",
                    "tag",
                    "priority",
                    "sign",
                    "valid_from",
                    "valid_to",
                ],
            ),
        )
        install_macros(con)
        rows = con.execute(
            """
            SELECT cik, canonical, period_end, value, filed_date, accession_number
            FROM fundamentals_asof(?) ORDER BY cik, canonical, period_end DESC
            """,
            [week],
        ).fetchall()
    facts: dict = defaultdict(lambda: defaultdict(list))
    for cik, concept, period, value, filed, accession in rows:
        facts[cik][concept].append(Filing(period, value, filed, accession))
    return facts, hashes


def archive_report(directory: Path, brief: dict, bars: Bars) -> Path:
    """Publish once, atomically. Readers see either the complete ZIP or no ZIP."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{brief['week']}.zip"
    with TemporaryDirectory(dir=directory) as stage:
        tmp = Path(stage) / "brief.zip"
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            with bundle.open("prices.csv", "w") as raw, io.TextIOWrapper(raw, newline="") as text:
                writer = csv.writer(text)
                writer.writerow(COLUMNS)
                for symbol, series in sorted(bars.items()):
                    for day, bar in sorted(series.items()):
                        writer.writerow((symbol, day, bar.raw, bar.split, bar.adjusted))
            bundle.writestr("brief.json", json.dumps(brief, allow_nan=False))
        with suppress(FileExistsError):
            os.link(tmp, target)
    return target


def read_brief(path: Path) -> dict:
    with zipfile.ZipFile(path) as bundle:
        return json.loads(bundle.read("brief.json"))


def generate(
    settings: Settings, *, scheduled: date | None = None, prices: Path | None = None
) -> Path:
    from finlab.ingest.universe import load_poc_universe

    now = datetime.now(UTC)
    latest = completed_friday(now)
    # The weekly daemon runs Saturday UTC. Preserve its original decision week on retries.
    week = scheduled - timedelta(days=(scheduled.weekday() - 4) % 7) if scheduled else latest
    if week > latest:
        raise ValueError("The requested US trading week has not closed.")
    directory = settings.reports_dir / "weekly"
    target = directory / f"{week}.zip"
    if target.is_file():
        return target
    companies = load_poc_universe()
    if prices:
        bars = read_csv(prices, week)
        source = f"Imported daily USD prices: {prices.name}"
        source_digest = hashlib.sha256(prices.read_bytes()).hexdigest()
    else:
        if not settings.alpaca_key_id or not settings.alpaca_secret_key:
            raise ValueError(
                "Set FINLAB_ALPACA_KEY_ID and FINLAB_ALPACA_SECRET_KEY, or pass --prices-csv."
            )
        with httpx.Client(
            timeout=45,
            headers={
                "APCA-API-KEY-ID": settings.alpaca_key_id.get_secret_value(),
                "APCA-API-SECRET-KEY": settings.alpaca_secret_key.get_secret_value(),
            },
        ) as client:
            bars = download(client, [BENCHMARK, *(c.ticker for c in companies)], week)
        source, source_digest = "Alpaca IEX daily bars (USD)", None
    facts, hashes = _annual_facts(settings, companies, week)
    rows = screen(companies, bars, facts, week)
    if not any("tests" in row for row in rows):
        raise ValueError(
            "No issuer has sufficient data for all four tests. Check the stock price coverage and annual filings, then retry."
        )
    brief = {
        "week": week.isoformat(),
        "generated_at": now.isoformat(),
        "model": MODEL,
        "source": source,
        "csv_sha256": source_digest,
        "sec_document_hashes": hashes,
        "benchmark": BENCHMARK,
        "rows": rows,
    }
    return archive_report(directory, brief, bars)
