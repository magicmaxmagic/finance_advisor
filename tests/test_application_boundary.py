"""Tests enforcing the Data Engine / Analytics architectural boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from finlab.analytics.snapshot import SnapshotUnavailable, require_valid_snapshot
from finlab.engine.snapshot import publish_snapshot, read_latest_snapshot


def test_snapshot_publish_is_atomic_and_readable(tmp_path: Path):
    warehouse = tmp_path / "warehouse"
    for name in (
        "entity",
        "filing",
        "security",
        "concept_map",
        "fundamentals_q4",
        "metric",
        "fact",
        "price_daily",
        "universe_snapshot",
    ):
        (warehouse / name).mkdir(parents=True)
        (warehouse / name / "part-0.parquet").write_bytes(b"fixture")

    snapshot = publish_snapshot(warehouse_dir=warehouse, schema_version=2)
    latest = read_latest_snapshot(warehouse)

    assert latest is not None
    assert latest.epoch == snapshot.epoch
    assert (warehouse / "snapshots" / f"{snapshot.epoch}.json").is_file()
    assert require_valid_snapshot(warehouse)["epoch"] == snapshot.epoch


def test_incomplete_snapshot_is_rejected(tmp_path: Path):
    warehouse = tmp_path / "warehouse"
    (warehouse / "entity").mkdir(parents=True)

    snapshot = publish_snapshot(warehouse_dir=warehouse, schema_version=2)
    assert snapshot.dataset_status["entity"] == "ready"

    with pytest.raises(SnapshotUnavailable, match="incomplete"):
        require_valid_snapshot(warehouse)


def test_analytics_package_forbids_writer_and_provider_imports():
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        "write_conn",
        "finlab.ingest.live_facts",
        "finlab.ingest.live_market",
        "finlab.ingest.yahoo",
    )
    for path in (root / "src/finlab/analytics").glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        imports = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)}
        froms = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        text = path.read_text()
        assert not any(token in imports or token in froms or token in text for token in forbidden)
