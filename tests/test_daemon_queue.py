"""The job queue state machine.

The four properties M4's acceptance criteria name, each tested directly:
killing mid-job leaves work recoverable, enqueueing twice makes one row,
simulated 429s back off without crashing a worker, and three strikes deadletter.
"""

from __future__ import annotations

from datetime import timedelta

import duckdb
import pytest

from finlab.daemon import queue
from finlab.daemon.pools import QuotaExhausted, parse_retry_after
from finlab.db.migrations import apply_baseline


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    apply_baseline(connection)
    return connection


# --- idempotent enqueue -----------------------------------------------------


def test_enqueueing_the_same_job_twice_makes_one_row(con: duckdb.DuckDBPyConnection) -> None:
    first = queue.enqueue(con, "fetch_filing", {"cik": 320193})
    second = queue.enqueue(con, "fetch_filing", {"cik": 320193})
    assert first == second
    assert con.execute("SELECT count(*) FROM job").fetchone()[0] == 1


def test_payload_key_order_does_not_change_the_id() -> None:
    """Dict ordering must not be able to produce two ids for one job."""
    assert queue.make_job_id("x", {"a": 1, "b": 2}) == queue.make_job_id("x", {"b": 2, "a": 1})


def test_different_payloads_are_different_jobs(con: duckdb.DuckDBPyConnection) -> None:
    queue.enqueue(con, "fetch_filing", {"cik": 1})
    queue.enqueue(con, "fetch_filing", {"cik": 2})
    assert con.execute("SELECT count(*) FROM job").fetchone()[0] == 2


# --- claiming ---------------------------------------------------------------


def test_claim_takes_the_highest_priority_job_first(con: duckdb.DuckDBPyConnection) -> None:
    queue.enqueue(con, "low", {"n": 1}, priority=200)
    queue.enqueue(con, "high", {"n": 2}, priority=10)
    job = queue.claim(con)
    assert job is not None and job.job_type == "high"


def test_a_claimed_job_is_not_claimed_again(con: duckdb.DuckDBPyConnection) -> None:
    queue.enqueue(con, "only", {"n": 1})
    assert queue.claim(con) is not None
    assert queue.claim(con) is None, "two workers must not get the same job"


def test_claim_respects_not_before(con: duckdb.DuckDBPyConnection) -> None:
    from datetime import UTC, datetime

    # UTC-naive, matching what the queue writes. Local time here would be an
    # hour in the *past* west of Greenwich, and the test would pass for the
    # wrong reason.
    future = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    queue.enqueue(con, "later", {"n": 1}, not_before=future)
    assert queue.claim(con) is None, "a backed-off job must not be runnable yet"


def test_claim_can_filter_by_job_type(con: duckdb.DuckDBPyConnection) -> None:
    queue.enqueue(con, "unknown_type", {"n": 1}, priority=1)
    queue.enqueue(con, "known_type", {"n": 2}, priority=100)
    job = queue.claim(con, job_types=["known_type"])
    assert job is not None and job.job_type == "known_type"


def test_claim_returns_none_on_an_empty_queue(con: duckdb.DuckDBPyConnection) -> None:
    assert queue.claim(con) is None


# --- crash recovery ---------------------------------------------------------


def test_a_job_abandoned_mid_run_is_recoverable(con: duckdb.DuckDBPyConnection) -> None:
    """Killing the daemon mid-job must not strand the row in `running` forever."""
    queue.enqueue(con, "work", {"n": 1})
    claimed = queue.claim(con)
    assert claimed is not None
    assert queue.counts(con) == {"running": 1}

    # The worker dies here: no complete(), no fail().
    reclaimed = queue.reclaim_stale(con, older_than=timedelta(0))

    assert reclaimed == 1
    assert queue.counts(con) == {"pending": 1}
    assert queue.claim(con) is not None, "the work must be runnable again"


def test_a_healthy_running_job_is_not_stolen(con: duckdb.DuckDBPyConnection) -> None:
    queue.enqueue(con, "work", {"n": 1})
    queue.claim(con)
    assert queue.reclaim_stale(con, older_than=timedelta(minutes=30)) == 0


# --- failure and backoff ----------------------------------------------------


def test_three_strikes_then_deadletter(con: duckdb.DuckDBPyConnection) -> None:
    queue.enqueue(con, "doomed", {"n": 1})
    states = []
    for _ in range(queue.MAX_ATTEMPTS):
        job = queue.claim(con)
        assert job is not None
        states.append(queue.fail(con, job, "boom"))
        con.execute("UPDATE job SET not_before = TIMESTAMP '2000-01-01'")

    assert states == ["pending", "pending", "deadletter"]
    assert queue.claim(con) is None, "a deadlettered job must not run again"


def test_backoff_grows_and_is_capped() -> None:
    assert queue.backoff_seconds(1) == 60
    assert queue.backoff_seconds(2) == 240
    assert queue.backoff_seconds(3) == 960
    assert queue.backoff_seconds(99) == queue.BACKOFF_MAX_SECONDS


def test_failure_pushes_the_retry_into_the_future(con: duckdb.DuckDBPyConnection) -> None:
    queue.enqueue(con, "flaky", {"n": 1})
    job = queue.claim(con)
    assert job is not None
    queue.fail(con, job, "transient")
    assert queue.claim(con) is None, "the retry must wait for its backoff"


# --- rate limiting ----------------------------------------------------------


def test_a_429_defers_without_spending_an_attempt(con: duckdb.DuckDBPyConnection) -> None:
    """Quota exhaustion is a scheduling fact, not the job's fault.

    If a 429 consumed an attempt, a busy afternoon would deadletter a day of
    work that was never broken.
    """
    queue.enqueue(con, "extract", {"section": "1A"})
    job = queue.claim(con)
    assert job is not None and job.attempts == 1

    queue.defer(con, job, seconds=300, reason="Ollama quota exhausted")

    attempts, state = con.execute("SELECT attempts, state FROM job").fetchone()
    assert attempts == 0, "the attempt must be given back"
    assert state == "pending"
    assert queue.claim(con) is None, "but it must not run until the backoff elapses"


def test_deferral_survives_repeatedly_without_deadlettering(
    con: duckdb.DuckDBPyConnection,
) -> None:
    queue.enqueue(con, "extract", {"section": "7"})
    for _ in range(10):
        con.execute("UPDATE job SET not_before = TIMESTAMP '2000-01-01'")
        job = queue.claim(con)
        assert job is not None, "a rate-limited job must never deadletter"
        queue.defer(con, job, seconds=60, reason="429")
    assert con.execute("SELECT state FROM job").fetchone()[0] == "pending"


def test_retry_after_header_is_honoured() -> None:
    import httpx

    response = httpx.Response(429, headers={"Retry-After": "120"})
    assert parse_retry_after(response, default=30.0) == 120.0


def test_missing_retry_after_is_jittered() -> None:
    """Three workers rejected at the same instant must not all return at once."""
    import httpx

    response = httpx.Response(429)
    values = {parse_retry_after(response, default=100.0) for _ in range(20)}
    assert len(values) > 1
    assert all(80.0 <= v <= 150.0 for v in values)


def test_quota_exhausted_carries_its_retry_delay() -> None:
    exc = QuotaExhausted(42.0, "slow down")
    assert exc.retry_after == 42.0
    assert "slow down" in str(exc)


# --- housekeeping -----------------------------------------------------------


def test_completed_jobs_are_purged_after_their_window(con: duckdb.DuckDBPyConnection) -> None:
    queue.enqueue(con, "work", {"n": 1})
    job = queue.claim(con)
    assert job is not None
    queue.complete(con, job.job_id)
    assert queue.purge_done(con, older_than=timedelta(days=7)) == 0
    assert queue.purge_done(con, older_than=timedelta(0)) == 1
