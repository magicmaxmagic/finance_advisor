"""Offline warehouse preparation and complete metadata publication."""

import duckdb

from finlab.config import Settings
from finlab.core.concepts import load_concept_map
from finlab.core.metrics import compute_metrics, install_metric_macros, store_metrics
from finlab.core.pit import build_q4_table, install_macros
from finlab.db.conn import read_conn
from finlab.db.migrations import current_version
from finlab.engine.snapshot import publish_snapshot
from finlab.engine.status import require_sec_ready
from finlab.ingest.warehouse import publish_table


def prepare_warehouse(con: duckdb.DuckDBPyConnection, settings: Settings, as_of):
    """Caller owns writer lock. Never publish when preparation raises."""
    require_sec_ready(settings.data_dir / "control")
    load_concept_map(con)
    for table in (
        "entity",
        "security",
        "filing",
        "concept_map",
        "corporate_action",
        "price_ingestion",
        "ticker_history",
    ):
        publish_table(con, source=table, root=settings.warehouse_dir / table)
    with read_conn(settings) as reader:
        build_q4_table(reader)
        publish_table(
            reader, source="fundamentals_q4", root=settings.warehouse_dir / "fundamentals_q4"
        )
    with read_conn(settings) as reader:
        install_macros(reader)
        install_metric_macros(reader)
        frame = compute_metrics(reader, as_of)
    store_metrics(con, frame, as_of)
    publish_table(con, source="metric", root=settings.warehouse_dir / "metric")
    return publish_snapshot(
        warehouse_dir=settings.warehouse_dir, schema_version=current_version(con)
    )
