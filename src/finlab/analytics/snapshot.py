"""Read-only snapshot validation for Analytics consumers."""

from __future__ import annotations

from pathlib import Path

from finlab.engine.snapshot import CONTRACT_VERSION, REQUIRED_DATASETS, read_latest_snapshot


class SnapshotUnavailable(RuntimeError):
    """No validated warehouse snapshot exists for Analytics."""


def require_valid_snapshot(warehouse_dir: Path) -> dict:
    """Require a complete, current-contract publication before analytics runs."""
    snapshot = read_latest_snapshot(warehouse_dir)
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
    return {
        "epoch": snapshot.epoch,
        "schema_version": snapshot.schema_version,
        "published_at": snapshot.published_at,
        "dataset_status": snapshot.dataset_status,
    }
