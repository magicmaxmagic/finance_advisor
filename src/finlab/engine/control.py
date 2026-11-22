"""Filesystem control inbox; only the daemon converts requests into DB jobs."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from finlab.daemon.queue import enqueue


def request_refresh(data_dir: Path) -> str:
    request_id = uuid4().hex
    inbox = data_dir / "control" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    temporary = inbox / f"{request_id}.tmp"
    temporary.write_text(
        json.dumps(
            {
                "request_id": request_id,
                "requested_at": datetime.now(UTC).isoformat(),
                "job_type": "prices",
            }
        )
    )
    temporary.replace(inbox / f"{request_id}.json")
    return request_id


def drain_requests(con, data_dir: Path) -> int:
    inbox = data_dir / "control" / "inbox"
    archive = data_dir / "control" / "requests"
    count = 0
    for path in sorted(inbox.glob("*.json"))[:100]:
        try:
            payload = json.loads(path.read_text())
            if payload.get("job_type") != "prices" or payload.get("request_id") != path.stem:
                raise ValueError("Invalid refresh request")
        except (ValueError, TypeError, AttributeError) as exc:
            rejected = data_dir / "control" / "rejected"
            rejected.mkdir(parents=True, exist_ok=True)
            path.replace(rejected / path.name)
            (rejected / (path.stem + ".error.txt")).write_text(str(exc))
            continue
        enqueue(con, "prices", {"request_id": payload["request_id"]}, priority=40)
        archive.mkdir(parents=True, exist_ok=True)
        path.replace(archive / path.name)
        count += 1
    return count
