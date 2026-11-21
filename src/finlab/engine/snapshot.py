"""Versioned, atomic publication contracts for FinLab's local warehouse."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

CONTRACT_VERSION = "1"
REQUIRED_DATASETS = (
    "entity",
    "filing",
    "security",
    "concept_map",
    "fundamentals_q4",
    "metric",
)


@dataclass(frozen=True, slots=True)
class WarehouseSnapshot:
    contract_version: str
    epoch: str
    schema_version: int
    published_at: str
    dataset_status: dict[str, str]
    checksums: dict[str, str]


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


def publish_snapshot(
    *,
    warehouse_dir: Path,
    schema_version: int,
) -> WarehouseSnapshot:
    """Atomically publish a manifest for a completed warehouse epoch."""
    now = datetime.now(UTC)
    epoch = now.strftime("%Y%m%dT%H%M%SZ")
    dataset_status = {
        name: "ready" if (warehouse_dir / name).is_dir() else "missing"
        for name in (*REQUIRED_DATASETS, "fact", "price_daily", "universe_snapshot")
    }
    checksums = {name: _tree_checksum(warehouse_dir / name) for name in dataset_status}
    snapshot = WarehouseSnapshot(
        contract_version=CONTRACT_VERSION,
        epoch=epoch,
        schema_version=schema_version,
        published_at=now.isoformat(),
        dataset_status=dataset_status,
        checksums=checksums,
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


def read_latest_snapshot(warehouse_dir: Path) -> WarehouseSnapshot | None:
    """Read the latest analytics-compatible publication epoch."""
    path = warehouse_dir / "snapshots" / "latest.json"
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
