"""Stage locally reviewed official calendar observations for publication."""

from datetime import date, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import polars as pl

from finlab.db.conn import parquet_files
from finlab.ingest.warehouse import publish_table

KINDS = {"earnings_release", "earnings_call", "investor_event", "dividend", "corporate_action"}


def _https(value):
    info = urlparse(value)
    if info.scheme != "https" or not info.hostname or info.username or info.password:
        raise ValueError("Events require an attributed HTTPS source")
    return info.hostname


def validate_event(row):
    """Date-only observations retain that precision. Future estimates require a source."""
    day = date.fromisoformat(row["event_date"])
    announced = date.fromisoformat(row["announced_on"])
    if row["kind"] not in KINDS or row["status"] not in {"confirmed", "estimated", "historical"}:
        raise ValueError("Unknown event type or evidence status")
    if not row["source"].strip() or not row["label"].strip() or int(row["cik"]) <= 0:
        raise ValueError("An issuer, source, and label are required")
    host = _https(row["source_url"])
    if row["status"] != "historical" and announced > day:
        raise ValueError("An announcement cannot confirm a past schedule")
    event_at = row.get("event_at")
    zone_name = row.get("timezone")
    if event_at:
        if not zone_name:
            raise ValueError("Timed events require a documented timezone")
        moment = datetime.fromisoformat(event_at)
        if moment.tzinfo is None or moment.astimezone(ZoneInfo(zone_name)).date() != day:
            raise ValueError("Event timestamp does not match the source calendar date")
    occurred = row.get("occurred_on")
    if occurred:
        occurred = date.fromisoformat(occurred)
        if occurred < day or not row.get("occurrence_url"):
            raise ValueError("Occurrence requires evidence at or after the event date")
        _https(row["occurrence_url"])
    if row["status"] == "historical" and not occurred:
        raise ValueError("Historical event requires occurrence evidence")
    fiscal = row.get("statement_period_end")
    if fiscal and date.fromisoformat(fiscal) > day:
        raise ValueError("The linked fiscal period cannot end after the event")
    return {
        "event_id": f"{row['cik']}:{row['kind']}:{row['event_date']}:{host}",
        "cik": int(row["cik"]),
        "kind": row["kind"],
        "label": row["label"],
        "event_date": str(day),
        "announced_on": str(announced),
        "status": row["status"],
        "source": row["source"],
        "source_url": row["source_url"],
        "timezone": zone_name,
        "event_at": event_at,
        "occurred_on": str(occurred) if occurred else None,
        "occurrence_url": row.get("occurrence_url"),
        "statement_period_end": fiscal,
    }


def stage_financial_events(con, settings, rows):
    evidence = [validate_event(row) for row in rows]
    if not evidence or len({item["event_id"] for item in evidence}) != len(evidence):
        raise ValueError("Provide a nonempty batch with unique event identities")
    ciks = [item["cik"] for item in evidence]
    if {
        row[0]
        for row in con.execute(
            "SELECT cik FROM entity WHERE cik IN (SELECT unnest(?))", [ciks]
        ).fetchall()
    } != set(ciks):
        raise ValueError("An event issuer is absent from the working catalogue")
    root = settings.warehouse_dir / "financial_event"
    frame = pl.DataFrame(
        evidence,
        schema_overrides={
            key: pl.String
            for key in (
                "timezone",
                "event_at",
                "occurred_on",
                "occurrence_url",
                "statement_period_end",
            )
        },
    )
    con.register("_incoming_events", frame)
    try:
        previous = parquet_files(root)
        con.execute("DROP TABLE IF EXISTS _working_events")
        if previous:
            con.execute(
                """CREATE TEMP TABLE _working_events AS
                SELECT * FROM read_parquet(?) WHERE event_id NOT IN (SELECT event_id FROM _incoming_events)
                UNION ALL BY NAME SELECT * FROM _incoming_events""",
                [[str(path) for path in previous]],
            )
        else:
            con.execute("CREATE TEMP TABLE _working_events AS SELECT * FROM _incoming_events")
        publish_table(
            con, source="_working_events", root=root, order_by=("cik", "event_date", "kind")
        )
    finally:
        con.unregister("_incoming_events")
    return len(evidence)
