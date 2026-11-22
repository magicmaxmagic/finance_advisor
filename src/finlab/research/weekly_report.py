"""Fetch current inputs, apply the existing PIT macro, and archive each published brief."""

from __future__ import annotations

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

from finlab.ingest.weekly_prices import COLUMNS, read_csv
from finlab.research.weekly import BENCHMARK, MODEL, Bars, Filing, completed_friday, screen

if TYPE_CHECKING:
    from finlab.config import Settings


def _annual_facts(settings: Settings, companies: tuple, week: date) -> tuple[dict, dict]:
    from finlab.analytics.snapshot import published_read_conn
    from finlab.core.pit import install_macros

    facts = defaultdict(lambda: defaultdict(list))
    with published_read_conn(settings) as con:
        install_macros(con)
        epoch = con.execute("SELECT epoch FROM _warehouse_publication").fetchone()[0]
        rows = con.execute(
            "SELECT cik,canonical,period_end,value,filed_date,accession_number FROM fundamentals_asof(?) WHERE duration_bucket='annual' AND canonical IN ('revenue','eps_diluted') AND cik IN (SELECT unnest(?)) ORDER BY cik,canonical,period_end DESC",
            [week, [company.cik for company in companies]],
        ).fetchall()
    for cik, canonical, period, value, filed, accession in rows:
        facts[cik]["diluted_eps" if canonical == "eps_diluted" else canonical].append(
            Filing(period, value, filed, accession)
        )
    return facts, {"warehouse_epoch": epoch}


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
                pl.col("unit").first(),
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


def archive_report(
    directory: Path, brief: dict, bars: Bars, *, revision: str | None = None
) -> Path:
    """Publish once, atomically. Readers see either the complete ZIP or no ZIP."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (f"{brief['week']}-{revision}.zip" if revision else f"{brief['week']}.zip")
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
    if prices is not None and target.is_file():
        return target
    if prices is None:
        from finlab.analytics.recommendations import recommendation_report

        report = recommendation_report(week, settings=settings)
        from finlab.analytics.recommendations import archive_recommendations
        from finlab.analytics.research_context import compare_reports

        candidates = []
        for path in (settings.reports_dir / "recommendations").glob("*.json"):
            prior = json.loads(path.read_text())
            if (
                prior.get("as_of", "") < str(week)
                and prior.get("model_version") == report["model_version"]
                and "code_sha256" in prior
            ):
                candidates.append((prior["as_of"], path, prior))
        changes = {"status": "previous_report_unavailable"}
        if candidates:
            _, prior_path, prior = max(candidates, key=lambda item: (item[0], item[1].name))
            changes = compare_reports(prior, report)
            changes["previous_archive"] = prior_path.name
        archive_recommendations(report, settings.reports_dir / "recommendations")
        for row in report["rows"]:
            row["status"] = row["category"]
        brief = {
            **report,
            "score_changes": changes,
            "week": week.isoformat(),
            "generated_at": now.isoformat(),
            "model": report["model_version"],
            "source": "Validated local warehouse",
            "benchmark": BENCHMARK,
        }
        return archive_report(
            directory, brief, {}, revision=report["model_version"] + "-" + report["warehouse_epoch"]
        )
    companies = load_poc_universe()
    bars = read_csv(prices, week)
    source = f"Imported daily USD prices: {prices.name}"
    source_digest = hashlib.sha256(prices.read_bytes()).hexdigest()
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
