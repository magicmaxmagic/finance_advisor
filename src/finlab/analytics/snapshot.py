"""Read-only snapshot validation for Analytics consumers."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from finlab import config
from finlab.config import Settings
from finlab.db.conn import attach_warehouse_views as attach_working_views
from finlab.db.conn import new_read_conn
from finlab.engine.snapshot import (
    CONTRACT_VERSION,
    REQUIRED_DATASETS,
    _tree_checksum,
    read_latest_snapshot,
)


class SnapshotUnavailable(RuntimeError):
    """No validated warehouse snapshot exists for Analytics."""


def require_valid_snapshot(warehouse_dir: Path, epoch: str | None = None) -> dict:
    """Require a complete, current-contract publication before analytics runs."""
    if epoch is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", epoch):
        raise SnapshotUnavailable("Invalid warehouse epoch")
    try:
        snapshot = read_latest_snapshot(warehouse_dir, epoch)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise SnapshotUnavailable("Invalid warehouse publication manifest") from exc
    if snapshot is None:
        raise SnapshotUnavailable(
            "No published warehouse snapshot is available. Run the Data Engine publication command first."
        )
    if snapshot.contract_version != CONTRACT_VERSION:
        raise SnapshotUnavailable(
            f"Unsupported warehouse contract {snapshot.contract_version}; expected {CONTRACT_VERSION}."
        )
    missing = [name for name in REQUIRED_DATASETS if snapshot.dataset_status.get(name) != "ready"]
    if missing:
        raise SnapshotUnavailable(
            "Published snapshot is incomplete; missing datasets: " + ", ".join(missing)
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", snapshot.epoch):
        raise SnapshotUnavailable("Invalid warehouse epoch")
    root = warehouse_dir / "snapshots" / snapshot.epoch
    for name, status in snapshot.dataset_status.items():
        if name not in (
            *REQUIRED_DATASETS,
            "fact",
            "price_daily",
            "universe_snapshot",
            "corporate_action",
            "price_ingestion",
            "ticker_history",
        ):
            raise SnapshotUnavailable("Unknown published dataset")
        if status == "ready" and (
            not (root / name).is_dir()
            or snapshot.checksums.get(name) != _tree_checksum(root / name)
        ):
            raise SnapshotUnavailable(f"Published dataset integrity check failed: {name}")
    return {
        "root": root,
        "epoch": snapshot.epoch,
        "schema_version": snapshot.schema_version,
        "published_at": snapshot.published_at,
        "dataset_status": snapshot.dataset_status,
    }


@contextmanager
def published_read_conn(settings: Settings | None = None, *, epoch: str | None = None):
    """Pin all views to one validated immutable epoch for the connection lifetime."""
    cfg = settings or config.get_settings()
    snapshot = require_valid_snapshot(cfg.warehouse_dir, epoch)
    con = new_read_conn(cfg, attach_warehouse=False)
    try:
        attach_working_views(con, cfg, warehouse_dir=snapshot["root"])
        con.execute(
            "CREATE TEMP TABLE _warehouse_publication AS SELECT ?::VARCHAR AS epoch, ?::VARCHAR AS published_at",
            [snapshot["epoch"], snapshot["published_at"]],
        )
        yield con
    finally:
        con.close()


def attached_views(con, settings=None):
    """Inventory pinned views without reattaching mutable working datasets."""
    return [row[0] for row in con.execute("SHOW TABLES").fetchall()]
