"""Standalone Data Engine entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from finlab.config import get_settings
from finlab.db.conn import WriterLockHeld, write_conn
from finlab.db.migrations import current_version
from finlab.engine.snapshot import publish_snapshot
from finlab.engine.status import require_sec_ready

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command("status")
def status() -> None:
    """Show schema and the latest published warehouse snapshot."""
    from finlab.engine.snapshot import read_latest_snapshot

    settings = get_settings()
    snapshot = read_latest_snapshot(settings.warehouse_dir)
    if snapshot is None:
        typer.echo("No published warehouse snapshot")
    else:
        typer.echo(f"epoch={snapshot.epoch}")
        typer.echo(f"published_at={snapshot.published_at}")
        typer.echo(f"contract={snapshot.contract_version}")


@app.command("publish")
def publish() -> None:
    """Publish a validated warehouse snapshot under the writer lock."""
    settings = get_settings()
    settings.ensure_dirs()
    try:
        with write_conn(settings) as con:
            require_sec_ready(settings.data_dir / "control")
            version = current_version(con)
            snapshot = publish_snapshot(
                warehouse_dir=settings.warehouse_dir,
                schema_version=version,
            )
    except WriterLockHeld as exc:
        raise typer.Exit(str(exc)) from exc
    typer.echo(f"published epoch={snapshot.epoch} contract={snapshot.contract_version}")


@app.command("sync-prices")
def sync_prices_command() -> None:
    """Recover missing price tails with a recent-correction overlap."""
    from datetime import date

    from finlab.ingest.price_sync import sync_prices
    from finlab.ingest.yahoo import YahooProvider

    settings = get_settings()
    with write_conn(settings) as con:
        reports = sync_prices(con, settings, YahooProvider(), end=date.today())
    typer.echo(f"Synchronized {len(reports)} request windows; publication is separate.")


@app.command("sync")
def sync(full: bool = False) -> None:
    """Synchronize filings, prices and actions, then publish one complete epoch."""
    from finlab.daemon.main import handle_prices_yahoo

    settings = get_settings()
    with write_conn(settings) as con:
        typer.echo(handle_prices_yahoo(con, settings, {"full": full}))


@app.command("prepare")
def prepare() -> None:
    """Build canonical fundamentals and publish existing local history offline."""
    from datetime import date

    from finlab.engine.prepare import prepare_warehouse

    cfg = get_settings()
    with write_conn(cfg) as con:
        snapshot = prepare_warehouse(con, cfg, date.today())
    typer.echo(f"Prepared and published {snapshot.epoch}")


@app.command("sync-sec")
def sync_sec(full: bool = False) -> None:
    """Fetch and verify SEC statements under the writer lock, then publish."""
    from datetime import UTC, datetime

    from finlab.engine.financial_sync import SecSyncIncomplete, sync_financials
    from finlab.engine.prepare import prepare_warehouse

    settings = get_settings()
    try:
        with write_conn(settings) as con:
            count = sync_financials(con, settings, full=full)
            snapshot = prepare_warehouse(con, settings, datetime.now(UTC).date())
    except (SecSyncIncomplete, WriterLockHeld) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"SEC synchronization verified {count} affected issuers; epoch {snapshot.epoch}")


@app.command("audit-sec")
def audit_sec(record: bool = False) -> None:
    """Check cached SEC accessions offline. Optionally record gaps under the writer lock."""
    import json

    from finlab.engine.financial_sync import inspect_cached_financials
    from finlab.engine.status import write_sec_checkpoint

    settings = get_settings()
    try:
        if record:
            with write_conn(settings):
                report = inspect_cached_financials(settings)
                if report["pending"] or report["issuer_errors"]:
                    write_sec_checkpoint(settings.data_dir / "control", report)
        else:
            report = inspect_cached_financials(settings)
    except WriterLockHeld as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report, indent=2))


@app.command("stage-share-classes")
def stage_classes(accession: str) -> None:
    """Retrieve an identified SEC accession and stage reconciled classes for a clean publication."""
    import asyncio
    import json
    import xml.etree.ElementTree as ET

    import httpx

    from finlab.ingest.sec import SecClient, SecEtiquetteError
    from finlab.ingest.share_classes import share_class_filing, stage_share_classes

    settings = get_settings()

    async def read_filing(url):
        async with SecClient(settings) as client:
            return await client.get_bytes(url)

    try:
        with write_conn(settings) as con:
            _, _, _, url = share_class_filing(con, accession)
            body = asyncio.run(read_filing(url))
            if body is None:
                raise ValueError("The SEC filing was not found")
            rows = stage_share_classes(con, settings, accession, body)
    except (WriterLockHeld, ValueError, ET.ParseError, SecEtiquetteError, httpx.HTTPError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(rows, indent=2, default=str))


@app.command("stage-events")
def stage_events(document: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Stage reviewed event evidence transcribed from official issuer or SEC sources."""
    import json

    from finlab.ingest.financial_events import stage_financial_events

    settings = get_settings()
    with write_conn(settings) as con:
        count = stage_financial_events(con, settings, json.loads(document.read_text()))
    typer.echo(f"Staged {count} official calendar observations.")


@app.command("stage-revenue-breakdown")
def stage_revenue(accession: str) -> None:
    """Extract revenue from SEC filing bytes already archived by Data Engine."""
    import hashlib
    import re

    from finlab.ingest.revenue_breakdown import stage_revenue_breakdown

    settings = get_settings()
    if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
        raise typer.BadParameter("An SEC accession number is required.")
    documents = list((settings.raw_dir / "filing_documents").glob(f"{accession}-*.htm"))
    if len(documents) != 1:
        raise typer.BadParameter(
            "This accession requires exactly one known SEC document in the local archive."
        )
    source = documents[0].read_bytes()
    if documents[0].stem != f"{accession}-{hashlib.sha256(source).hexdigest()}":
        raise typer.BadParameter(
            "The local SEC document digest does not match its archived identity."
        )
    try:
        with write_conn(settings) as con:
            count = stage_revenue_breakdown(con, settings, accession, source)
    except (ValueError, WriterLockHeld) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Staged {count} reconciled revenue observations.")


@app.command("publish-evidence")
def publish_evidence() -> None:
    """Extend the current epoch with staged evidence while holding core datasets fixed."""
    from finlab.db.conn import attach_warehouse_views, new_read_conn, parquet_files
    from finlab.engine.snapshot import EVIDENCE_DATASETS, read_latest_snapshot

    settings = get_settings()
    warehouse = settings.warehouse_dir
    with write_conn(settings) as _:
        base = read_latest_snapshot(warehouse)
        if not base:
            raise typer.BadParameter("Evidence requires an existing publication.")
        root = warehouse / "snapshots" / base.epoch
        with new_read_conn(settings, attach_warehouse=False) as reader:
            attach_warehouse_views(reader, settings, warehouse_dir=root)
            has_evidence = False
            for name in sorted(EVIDENCE_DATASETS):
                files = [str(path) for path in parquet_files(warehouse / name)]
                if not files:
                    continue
                has_evidence = True
                if name in {"financial_event", "financial_text"}:
                    check = "SELECT count(*) FROM read_parquet(?) s ANTI JOIN entity e USING(cik)"
                else:
                    check = """SELECT count(*) FROM read_parquet(?) s ANTI JOIN filing f
                        ON s.cik=f.cik AND s.accession_number=f.accession_number
                        AND s.filed_date=f.filed_date AND s.period_end=f.period_of_report
                        AND s.source_url=f.primary_doc_url"""
                if reader.execute(check, [files]).fetchone()[0]:
                    raise typer.BadParameter(
                        f"{name} contains evidence outside the published catalogue."
                    )
            if not has_evidence:
                raise typer.BadParameter("No evidence is staged.")
        snapshot = publish_snapshot(
            warehouse_dir=warehouse,
            schema_version=base.schema_version,
            evidence_base=base,
        )
    typer.echo(f"Published evidence epoch {snapshot.epoch}; frozen financial base {base.epoch}.")


@app.command("sync-financial-news")
def sync_financial_news() -> None:
    """Archive the reviewed SEC earnings releases for local tone analysis."""
    import asyncio

    import httpx

    from finlab.ingest.financial_text import acquire_earnings_text
    from finlab.ingest.sec import SecEtiquetteError, sec_failure

    settings = get_settings()
    try:
        with write_conn(settings) as con:
            count, unavailable = asyncio.run(acquire_earnings_text(con, settings))
    except (SecEtiquetteError, WriterLockHeld) as exc:
        raise typer.BadParameter(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise typer.BadParameter(f"SEC releases unavailable: {sec_failure(exc).code}") from exc
    typer.echo(f"Staged {count} reviewed SEC narratives; {unavailable} unavailable.")


if __name__ == "__main__":
    app()
