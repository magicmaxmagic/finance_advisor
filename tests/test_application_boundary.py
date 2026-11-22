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


def test_epoch_contents_survive_working_updates(tmp_path: Path):
    """An open reader stays pinned even while another epoch is published."""
    import duckdb

    from finlab.analytics.snapshot import published_read_conn
    from finlab.config import Settings
    from finlab.engine.snapshot import REQUIRED_DATASETS

    cfg = Settings(data_dir=tmp_path)
    with duckdb.connect() as writer:
        for name in REQUIRED_DATASETS:
            root = cfg.warehouse_dir / name
            root.mkdir(parents=True)
            writer.execute(f"COPY (SELECT 1 AS value) TO '{root}/part.parquet' (FORMAT PARQUET)")
    first = publish_snapshot(warehouse_dir=cfg.warehouse_dir, schema_version=2)
    with published_read_conn(cfg) as reader:
        with duckdb.connect() as writer:
            path = cfg.warehouse_dir / "entity" / "part.parquet"
            path.unlink()
            writer.execute(f"COPY (SELECT 2 AS value) TO '{path}' (FORMAT PARQUET)")
        second = publish_snapshot(warehouse_dir=cfg.warehouse_dir, schema_version=2)
        assert first.epoch != second.epoch
        assert reader.execute("SELECT value FROM entity").fetchone() == (1,)
        with published_read_conn(cfg) as newer:
            assert newer.execute("SELECT value FROM entity").fetchone() == (2,)


def test_published_tampering_is_rejected(tmp_path: Path):
    from finlab.engine.snapshot import REQUIRED_DATASETS

    for name in REQUIRED_DATASETS:
        root = tmp_path / name
        root.mkdir()
        (root / "part.parquet").write_bytes(b"original")
    snapshot = publish_snapshot(warehouse_dir=tmp_path, schema_version=2)
    (tmp_path / "snapshots" / snapshot.epoch / "entity" / "part.parquet").chmod(0o644)
    (tmp_path / "snapshots" / snapshot.epoch / "entity" / "part.parquet").write_bytes(b"changed")
    with pytest.raises(SnapshotUnavailable, match="integrity"):
        require_valid_snapshot(tmp_path)


def test_api_fails_closed_without_publication(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    from finlab.api.app import app
    from finlab.config import Settings

    monkeypatch.setattr("finlab.config.get_settings", lambda: Settings(data_dir=tmp_path))
    client = TestClient(app)
    for route in ("/api/warehouse/status", "/api/warehouse/assets/AAPL", "/api/analytics/rankings"):
        response = client.get(route)
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"


def test_api_warehouse_routes_do_not_call_network(tmp_path: Path, monkeypatch):
    import socket

    import duckdb
    from fastapi.testclient import TestClient

    from finlab.api.app import app
    from finlab.config import Settings
    from finlab.engine.snapshot import REQUIRED_DATASETS

    cfg = Settings(data_dir=tmp_path)
    with duckdb.connect() as con:
        for name in REQUIRED_DATASETS:
            root = cfg.warehouse_dir / name
            root.mkdir(parents=True)
            con.execute(f"COPY (SELECT 1 AS value) TO '{root}/part.parquet' (FORMAT PARQUET)")
    publish_snapshot(warehouse_dir=cfg.warehouse_dir, schema_version=2)
    monkeypatch.setattr("finlab.config.get_settings", lambda: cfg)

    def deny_network(*args, **kwargs):
        raise AssertionError("Analytics attempted network access")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    response = TestClient(app).get("/api/warehouse/status")
    assert response.status_code == 200
    assert response.json()["datasets"]["entity"] == 1


def test_publications_reuse_immutable_objects_without_linking_working_files(tmp_path):
    from finlab.engine.snapshot import REQUIRED_DATASETS

    for name in REQUIRED_DATASETS:
        (tmp_path / name).mkdir()
        (tmp_path / name / "part.parquet").write_bytes(b"unchanged")
    first = publish_snapshot(warehouse_dir=tmp_path, schema_version=5)
    second = publish_snapshot(warehouse_dir=tmp_path, schema_version=5)
    old = tmp_path / "snapshots" / first.epoch / "entity/part.parquet"
    new = tmp_path / "snapshots" / second.epoch / "entity/part.parquet"
    working = tmp_path / "entity/part.parquet"
    assert old.stat().st_ino == new.stat().st_ino
    assert old.stat().st_ino != working.stat().st_ino
    working.write_bytes(b"revised")
    assert old.read_bytes() == b"unchanged"
    assert require_valid_snapshot(tmp_path)["epoch"] == second.epoch


def test_reader_ignores_interrupted_partition_trash(settings):
    import duckdb

    from finlab.db.conn import read_conn

    root = settings.warehouse_dir / "price_daily"
    (root / "year=2020").mkdir(parents=True)
    (root / ".trash-year=2020").mkdir()
    with duckdb.connect() as con:
        con.execute(
            f"COPY (SELECT 'A' AS security_id, DATE '2020-01-02' AS date,100.0 AS close) TO '{root}/year=2020/part.parquet' (FORMAT PARQUET)"
        )
        con.execute(
            f"COPY (SELECT 'A' AS security_id, DATE '2020-01-02' AS date,999.0 AS close) TO '{root}/.trash-year=2020/part.parquet' (FORMAT PARQUET)"
        )
    with read_conn(settings) as con:
        assert con.execute("SELECT close FROM price_daily").fetchall() == [(100.0,)]
