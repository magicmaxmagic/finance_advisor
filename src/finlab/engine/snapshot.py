"""Versioned, atomic publication contracts for FinLab's local warehouse."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from finlab.db.conn import parquet_files

CONTRACT_VERSION = "2"
REQUIRED_DATASETS = (
    "entity",
    "filing",
    "security",
    "concept_map",
    "fundamentals_q4",
    "metric",
)
OPTIONAL_DATASETS = (
    "fact",
    "price_daily",
    "universe_snapshot",
    "corporate_action",
    "price_ingestion",
    "ticker_history",
    "share_class_fact",
    "financial_event",
)
EVIDENCE_DATASETS = {"share_class_fact", "financial_event"}


@dataclass(frozen=True, slots=True)
class WarehouseSnapshot:
    contract_version: str
    epoch: str
    schema_version: int
    published_at: str
    dataset_status: dict[str, str]
    checksums: dict[str, str]
    base_epoch: str | None = None
    publication_scope: str = "full"


def _tree_checksum(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        digest.update(b"MISSING")
        return digest.hexdigest()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_file(source: Path, target: Path, objects: Path) -> None:
    digest = _file_hash(source)
    blob = objects / digest[:2] / digest
    blob.parent.mkdir(parents=True, exist_ok=True)
    if not blob.exists() or _file_hash(blob) != digest:
        temporary = blob.with_name("." + digest + "-" + uuid4().hex)
        shutil.copyfile(source, temporary)
        if _file_hash(temporary) != digest:
            temporary.unlink()
            raise RuntimeError("Working dataset changed during publication")
        temporary.chmod(0o444)
        temporary.replace(blob)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(blob, target)


def publish_snapshot(
    *,
    warehouse_dir: Path,
    schema_version: int,
    evidence_base: WarehouseSnapshot | None = None,
) -> WarehouseSnapshot:
    """Atomically publish a manifest for a completed warehouse epoch."""
    now = datetime.now(UTC)
    epoch = now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
    manifests = warehouse_dir / "snapshots"
    dataset_roots = {
        name: warehouse_dir / name for name in (*REQUIRED_DATASETS, *OPTIONAL_DATASETS)
    }
    if evidence_base:
        if evidence_base != read_latest_snapshot(warehouse_dir):
            raise ValueError("Evidence publication must extend the current immutable epoch.")
        base_root = manifests / evidence_base.epoch
        if any(
            _tree_checksum(base_root / name) != digest
            for name, digest in evidence_base.checksums.items()
        ):
            raise ValueError("Evidence publication requires an intact base epoch.")
        for name, root in dataset_roots.items():
            if name not in EVIDENCE_DATASETS or not root.is_dir():
                dataset_roots[name] = base_root / name
    dataset_status = {
        name: "ready" if root.is_dir() else "missing" for name, root in dataset_roots.items()
    }
    # The caller holds the warehouse writer lock throughout copying and publication.
    # Copies intentionally avoid hard links: a working file may later be overwritten.
    manifests.mkdir(parents=True, exist_ok=True)
    staging = manifests / f".{epoch}.staging"
    destination = manifests / epoch
    staging.mkdir()
    try:
        for name, status in dataset_status.items():
            if status == "ready":
                (staging / name).mkdir()
                root = dataset_roots[name]
                for source in parquet_files(root):
                    _publish_file(
                        source,
                        staging / name / source.relative_to(root),
                        manifests / ".objects",
                    )
        checksums = {name: _tree_checksum(staging / name) for name in dataset_status}
        if evidence_base and any(
            key not in EVIDENCE_DATASETS and checksums.get(key) != digest
            for key, digest in evidence_base.checksums.items()
        ):
            raise ValueError("Evidence publication changed a frozen base dataset.")
        staging.replace(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    snapshot = WarehouseSnapshot(
        contract_version=CONTRACT_VERSION,
        epoch=epoch,
        schema_version=schema_version,
        published_at=now.isoformat(),
        dataset_status=dataset_status,
        checksums=checksums,
        base_epoch=evidence_base.epoch if evidence_base else None,
        publication_scope="evidence_only" if evidence_base else "full",
    )

    manifests = warehouse_dir / "snapshots"
    manifests.mkdir(parents=True, exist_ok=True)
    final = manifests / f"{epoch}.json"
    temporary = manifests / f".{epoch}.tmp"
    temporary.write_text(json.dumps(asdict(snapshot), indent=2, sort_keys=True) + "\n")
    # Use Path replace for atomic snapshot publication
    temporary.replace(final)

    latest = manifests / "latest.json"
    latest_tmp = manifests / ".latest.tmp"
    latest_tmp.write_text(json.dumps(asdict(snapshot), indent=2, sort_keys=True) + "\n")
    # Use Path replace for atomic snapshot publication
    latest_tmp.replace(latest)
    return snapshot


def read_latest_snapshot(warehouse_dir: Path, epoch: str | None = None) -> WarehouseSnapshot | None:
    """Read the latest analytics-compatible publication epoch."""
    path = warehouse_dir / "snapshots" / (f"{epoch}.json" if epoch else "latest.json")
    if not path.is_file():
        return None
    payload = json.loads(path.read_text())
    return WarehouseSnapshot(
        contract_version=str(payload["contract_version"]),
        epoch=str(payload["epoch"]),
        schema_version=int(payload["schema_version"]),
        published_at=str(payload["published_at"]),
        dataset_status=dict(payload["dataset_status"]),
        checksums=dict(payload["checksums"]),
    )
