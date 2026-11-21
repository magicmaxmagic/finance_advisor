"""Ingestion status snapshots published by the Data Engine."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class IngestionStep:
    name: str
    state: str
    started_at: str | None
    finished_at: str | None
    elapsed_seconds: float | None
    estimated_seconds: float | None
    progress: float
    records: int
    bytes: int
    detail: str


@dataclass(frozen=True, slots=True)
class IngestionStatus:
    updated_at: str
    pipeline_state: str
    steps: list[IngestionStep]


def write_status(settings_dir: Path, status: IngestionStatus) -> Path:
    """Atomically publish an ingestion status file for read-only consumers."""
    target = settings_dir / "status.json"
    settings_dir.mkdir(parents=True, exist_ok=True)
    temporary = settings_dir / ".status.tmp"
    temporary.write_text(json.dumps(asdict(status), indent=2, sort_keys=True) + "\n")
    temporary.replace(target)
    return target


def read_status(settings_dir: Path) -> dict:
    target = settings_dir / "status.json"
    if not target.is_file():
        return {
            "updated_at": datetime.now(UTC).isoformat(),
            "pipeline_state": "idle",
            "steps": [],
        }
    return json.loads(target.read_text())
