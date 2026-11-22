"""Ingestion status snapshots published by the Data Engine."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
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
    payload = asdict(status)
    try:
        previous = json.loads(target.read_text())
        history = previous.get("recent_jobs", [])
        if not isinstance(history, list):
            history = []
    except (FileNotFoundError, json.JSONDecodeError, AttributeError):
        history = []
    completed = [asdict(step) for step in status.steps if step.finished_at is not None]
    seen = {
        (row.get("name"), row.get("started_at"), row.get("state"))
        for row in history
        if isinstance(row, dict)
    }
    history.extend(
        row for row in completed if (row["name"], row["started_at"], row["state"]) not in seen
    )
    payload["recent_jobs"] = history[-50:]
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(target)
    return target


def read_status(settings_dir: Path) -> dict:
    target = settings_dir / "status.json"
    if not target.is_file():
        return {
            "updated_at": None,
            "pipeline_state": "not_started",
            "steps": [],
        }
    return json.loads(target.read_text())
