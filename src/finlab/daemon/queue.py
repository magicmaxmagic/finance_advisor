"""The `job` table state machine and atomic claim.

    pending -> running -> done | failed | deadletter

Three properties this has to get right, because the daemon is the only writer
and a stuck queue is silent:

**Claiming is atomic.** `UPDATE ... RETURNING` moves a row to `running` and
returns it in one statement, so two workers cannot take the same job even
though they share one connection.

**Enqueueing is idempotent.** `job_id` is a hash of the job's natural key, and
inserts use `INSERT OR IGNORE`. Re-running the daily schedule, or retrying after
a crash mid-enqueue, produces one row rather than a duplicate backlog.

**A crash is recoverable.** A worker killed mid-job leaves its row in `running`
with a stale `locked_at`; nothing would ever move it again. `reclaim_stale`
sweeps those back to `pending` on startup and periodically after, which is why
the daemon can be killed at any point without losing work.

Retries back off through `not_before` rather than sleeping, so a rate-limited
job frees its worker instead of blocking it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb

MAX_ATTEMPTS = 3

# Long enough that a slow-but-alive job is not stolen, short enough that a
# crashed one comes back the same day.
STALE_AFTER = timedelta(minutes=30)

# 1 min, 4 min, 16 min. Capped so a persistently failing job still retries
# within a scheduling window rather than drifting into next week.
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    job_type: str
    payload: dict[str, Any]
    attempts: int
    priority: int


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def make_job_id(job_type: str, payload: dict[str, Any] | None = None) -> str:
    """A stable id derived from the job's natural key.

    Same type and same payload means the same job, so enqueueing twice is a
    no-op rather than duplicated work. Keys are sorted so dict ordering cannot
    produce two ids for one job.
    """
    body = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{job_type}\x1f{body}".encode()).hexdigest()[:32]


def _now() -> datetime:
    """UTC, naive.

    Every timestamp this module writes or compares comes from here. SQL's
    `now()` is local time, so mixing the two silently shifted `not_before` by
    the UTC offset and made freshly-enqueued jobs unclaimable for hours.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def backoff_seconds(attempts: int) -> int:
    return min(BACKOFF_BASE_SECONDS * (4 ** max(0, attempts - 1)), BACKOFF_MAX_SECONDS)


def enqueue(
    con: duckdb.DuckDBPyConnection,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    priority: int = 100,
    not_before: datetime | None = None,
) -> str:
    """Add a job unless an identical one already exists. Returns the job_id."""
    job_id = make_job_id(job_type, payload)
    con.execute(
        """
        INSERT OR IGNORE INTO job
          (job_id, job_type, payload, priority, state, attempts, not_before,
           locked_by, locked_at, error, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, ?, ?)
        """,
        [
            job_id,
            job_type,
            json.dumps(payload or {}, sort_keys=True),
            priority,
            not_before or _now(),
            _now(),
            _now(),
        ],
    )
    return job_id


def claim(con: duckdb.DuckDBPyConnection, *, job_types: list[str] | None = None) -> Job | None:
    """Take the highest-priority runnable job, atomically.

    The UPDATE and the read are one statement: a job cannot be observed as
    pending by one worker while another is already flipping it to running.
    """
    now = _now()
    predicate = ""
    params: list[Any] = [worker_id(), now, now, now]
    if job_types:
        placeholders = ", ".join(["?"] * len(job_types))
        predicate = f"AND job_type IN ({placeholders})"
        params.extend(job_types)

    row = con.execute(
        f"""
        UPDATE job SET
          state = 'running',
          attempts = attempts + 1,
          locked_by = ?,
          locked_at = ?,
          updated_at = ?
        WHERE job_id = (
          SELECT job_id FROM job
          WHERE state = 'pending' AND (not_before IS NULL OR not_before <= ?)
          {predicate}
          ORDER BY priority ASC, created_at ASC
          LIMIT 1
        )
        RETURNING job_id, job_type, payload, attempts, priority
        """,
        params,
    ).fetchone()
    if row is None:
        return None
    return Job(
        job_id=row[0],
        job_type=row[1],
        payload=json.loads(row[2]) if row[2] else {},
        attempts=int(row[3]),
        priority=int(row[4]),
    )


def complete(con: duckdb.DuckDBPyConnection, job_id: str) -> None:
    con.execute(
        "UPDATE job SET state = 'done', locked_by = NULL, locked_at = NULL, "
        "error = NULL, updated_at = ? WHERE job_id = ?",
        [_now(), job_id],
    )


def fail(con: duckdb.DuckDBPyConnection, job: Job, error: str) -> str:
    """Record a failure. Returns the resulting state.

    Three strikes and the job goes to `deadletter` rather than retrying forever
    — a job that has failed three times is a bug report, not a transient.
    """
    state = "deadletter" if job.attempts >= MAX_ATTEMPTS else "pending"
    retry_at = _now() + timedelta(seconds=backoff_seconds(job.attempts))
    con.execute(
        "UPDATE job SET state = ?, not_before = ?, locked_by = NULL, locked_at = NULL, "
        "error = ?, updated_at = ? WHERE job_id = ?",
        [state, retry_at, error[:2000], _now(), job.job_id],
    )
    return state


def defer(con: duckdb.DuckDBPyConnection, job: Job, seconds: float, reason: str) -> None:
    """Push a job into the future without counting it as a failure.

    This is what a 429 maps to. Quota exhaustion is not the job's fault and must
    not consume one of its three attempts, or a busy afternoon would deadletter
    a day's work.
    """
    con.execute(
        "UPDATE job SET state = 'pending', attempts = attempts - 1, not_before = ?, "
        "locked_by = NULL, locked_at = NULL, error = ?, updated_at = ? WHERE job_id = ?",
        [_now() + timedelta(seconds=seconds), reason[:2000], _now(), job.job_id],
    )


def reclaim_stale(con: duckdb.DuckDBPyConnection, *, older_than: timedelta = STALE_AFTER) -> int:
    """Return jobs abandoned by a dead worker to `pending`.

    Without this a SIGKILL mid-job strands the row in `running` forever, and the
    queue quietly stops making progress on that work.
    """
    cutoff = _now() - older_than
    rows = con.execute(
        "UPDATE job SET state = 'pending', locked_by = NULL, locked_at = NULL, "
        "error = 'reclaimed after worker went away', updated_at = ? "
        "WHERE state = 'running' AND locked_at IS NOT NULL AND locked_at < ? "
        "RETURNING job_id",
        [_now(), cutoff],
    ).fetchall()
    return len(rows)


def counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    return {
        row[0]: int(row[1])
        for row in con.execute("SELECT state, count(*) FROM job GROUP BY state").fetchall()
    }


def purge_done(con: duckdb.DuckDBPyConnection, *, older_than: timedelta) -> int:
    rows = con.execute(
        "DELETE FROM job WHERE state = 'done' AND updated_at < ? RETURNING job_id",
        [_now() - older_than],
    ).fetchall()
    return len(rows)
