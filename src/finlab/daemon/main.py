"""`finlabd`: the single process holding the read-write connection.

One process, one DuckDB write connection, one job at a time. Job-level
parallelism is deliberately absent: there is only one writer, so two handlers
mutating the warehouse concurrently would have nothing to gain and a lock to
fight over. Where concurrency actually pays — many HTTP requests inside one job
— it lives inside the handler, bounded by `pools.Pools`.

Handlers run in a worker thread so a blocking download cannot stall the
scheduler, and a lock ensures only one of them holds the connection at a time.

Shutdown is graceful: SIGTERM stops the scheduler, lets the running job finish,
and releases the write lock. A job interrupted by SIGKILL instead is recovered
by `reclaim_stale` on the next start, which is why killing the daemon is safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import duckdb
import structlog

from finlab.config import Settings, get_settings
from finlab.daemon import queue
from finlab.daemon.pools import Pools, QuotaExhausted
from finlab.daemon.scheduler import SCHEDULE, build_scheduler
from finlab.db.conn import write_conn

log = structlog.get_logger()

Handler = Callable[[duckdb.DuckDBPyConnection, Settings, dict[str, Any]], str]

IDLE_SLEEP_SECONDS = 5.0
RECLAIM_EVERY = timedelta(minutes=10)
PURGE_DONE_AFTER = timedelta(days=7)


def configure_logging(settings: Settings) -> None:
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}[settings.log_level]
        ),
        cache_logger_on_first_use=True,
    )


# --- handlers ---------------------------------------------------------------
# Only deterministic work. No LLM job types until M5.


def handle_universe_snapshot(
    con: duckdb.DuckDBPyConnection, settings: Settings, payload: dict[str, Any]
) -> str:
    from finlab.ingest.universe import capture_snapshot, rebuild_securities

    report = capture_snapshot(con, settings)
    securities = rebuild_securities(con, settings)
    if report.skipped:
        return f"{report.snapshot_date} already captured; {securities} securities"
    return f"captured {report.snapshot_date}: {report.rows} securities, {securities} tracked"


def handle_submissions(
    con: duckdb.DuckDBPyConnection, settings: Settings, payload: dict[str, Any]
) -> str:
    from finlab.ingest.submissions import ingest_submissions

    report = ingest_submissions(con, settings, force=True, refresh_archives=False)
    return f"{report.filings_updated} filings carry accepted_at"


def handle_rebuild_derived(
    con: duckdb.DuckDBPyConnection, settings: Settings, payload: dict[str, Any]
) -> str:
    from finlab.core.concepts import load_concept_map
    from finlab.core.metrics import compute_metrics, install_metric_macros, store_metrics
    from finlab.core.pit import analytics_conn, build_q4_table
    from finlab.ingest.warehouse import publish_table

    mappings = load_concept_map(con)
    publish_table(
        con,
        source="concept_map",
        root=settings.warehouse_dir / "concept_map",
        order_by=("canonical", "priority"),
    )
    with analytics_conn(settings) as reader:
        q4 = build_q4_table(reader)
        publish_table(
            reader,
            source="fundamentals_q4",
            root=settings.warehouse_dir / "fundamentals_q4",
            order_by=("cik", "canonical", "period_end"),
        )
        install_metric_macros(reader)
        today = datetime.now(UTC).date()
        frame = compute_metrics(reader, today)
    store_metrics(con, frame, today)
    publish_table(
        con,
        source="metric",
        root=settings.warehouse_dir / "metric",
        order_by=("cik", "as_of_date", "metric_name"),
    )
    return f"{mappings} mappings, {q4} Q4 rows, {len(frame)} metrics"


def handle_coverage_check(
    con: duckdb.DuckDBPyConnection, settings: Settings, payload: dict[str, Any]
) -> str:
    from finlab.core.concepts import COVERAGE_THRESHOLD, coverage
    from finlab.core.pit import analytics_conn

    with analytics_conn(settings) as reader:
        rows = coverage(reader, datetime.now(UTC).date())
    failing = [r for r in rows if not r.passing]
    if failing:
        # Reported, not raised: coverage is a standing property to watch, and
        # deadlettering a weekly check would just hide it.
        log.warning(
            "concept coverage below threshold",
            threshold=COVERAGE_THRESHOLD,
            failing={r.canonical: round(r.fraction, 3) for r in failing},
        )
    return f"{len(rows) - len(failing)}/{len(rows)} concepts at or above threshold"


def handle_prices_yahoo(
    con: duckdb.DuckDBPyConnection, settings: Settings, payload: dict[str, Any]
) -> str:
    from finlab.engine.prepare import prepare_warehouse
    from finlab.ingest.calendar import last_completed_session
    from finlab.ingest.price_sync import sync_prices
    from finlab.ingest.yahoo import YahooProvider

    end = last_completed_session(datetime.now(UTC))
    if not settings.sec_user_agent_is_placeholder:
        from finlab.engine.financial_sync import sync_financials

        sync_financials(con, settings)
    reports = sync_prices(
        con, settings, YahooProvider(timeout=30.0), end=end, full=bool(payload.get("full"))
    )
    snapshot = prepare_warehouse(con, settings, end)
    return (
        f"Yahoo incremental refresh epoch {snapshot.epoch}: {len(reports)} windows, "
        f"{sum(report.corporate_actions for report in reports)} actions"
    )


def handle_price_corrections(con, settings, payload):
    return handle_prices_yahoo(con, settings, {**payload, "full": True})


def handle_companyfacts_refresh(con, settings, payload):
    from finlab.engine.financial_sync import sync_financials
    from finlab.engine.prepare import prepare_warehouse

    count = sync_financials(con, settings, full=True)
    snapshot = prepare_warehouse(con, settings, datetime.now(UTC).date())
    return f"SEC correction refresh: {count} companies, epoch {snapshot.epoch}"


def handle_edgar_index(con, settings, payload):
    from finlab.engine.financial_sync import sync_financials
    from finlab.engine.prepare import prepare_warehouse

    count = sync_financials(con, settings)
    snapshot = prepare_warehouse(con, settings, datetime.now(UTC).date())
    return f"SEC filing updates: {count} affected companies, epoch {snapshot.epoch}"


HANDLERS: dict[str, Handler] = {
    "companyfacts_refresh": handle_companyfacts_refresh,
    "edgar_index": handle_edgar_index,
    "prices": handle_prices_yahoo,
    "price_corrections": handle_price_corrections,
    "universe_snapshot": handle_universe_snapshot,
    "submissions": handle_submissions,
    "rebuild_derived": handle_rebuild_derived,
    "coverage_check": handle_coverage_check,
}


# --- the daemon -------------------------------------------------------------


class Daemon:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.pools = Pools(self.settings)
        self.stopping = asyncio.Event()
        self._db_lock = asyncio.Lock()
        self._last_reclaim = datetime.min

    def _enqueue_scheduled(
        self, con: duckdb.DuckDBPyConnection
    ) -> Callable[[str, int], Awaitable[None]]:
        async def enqueue_fn(job_type: str, priority: int) -> None:
            # The scheduled date is part of the payload so each day is a
            # distinct job. Without it every run would hash to one job_id and
            # INSERT OR IGNORE would silently drop every day after the first.
            today = datetime.now(UTC).date().isoformat()
            async with self._db_lock:
                job_id = queue.enqueue(con, job_type, {"scheduled_for": today}, priority=priority)
            log.info("scheduled", job_type=job_type, job_id=job_id, date=today)

        return enqueue_fn

    async def _run_job(self, con: duckdb.DuckDBPyConnection, job: queue.Job) -> None:
        from finlab.engine.financial_sync import SecSyncIncomplete

        handler = HANDLERS.get(job.job_type)
        if handler is None:
            queue.fail(con, job, f"no handler registered for {job.job_type!r}")
            log.error("unknown job type", job_type=job.job_type, job_id=job.job_id)
            return

        bound = log.bind(job_id=job.job_id, job_type=job.job_type, attempt=job.attempts)
        from finlab.engine.status import IngestionStatus, IngestionStep, write_status

        start_time = datetime.now(UTC).isoformat()

        def status(state, detail, elapsed=None):
            step = IngestionStep(
                job.job_type,
                state,
                start_time,
                datetime.now(UTC).isoformat() if state != "running" else None,
                elapsed,
                None,
                1.0 if state == "done" else 0.0,
                0,
                0,
                detail,
            )
            try:
                write_status(
                    self.settings.data_dir / "control",
                    IngestionStatus(datetime.now(UTC).isoformat(), state, [step]),
                )
            except OSError as exc:
                log.warning("status publication failed", error=str(exc))

        status("running", "Data Engine job in progress")
        bound.info("job started")
        started = asyncio.get_running_loop().time()
        try:
            detail = await asyncio.to_thread(handler, con, self.settings, job.payload)
        except QuotaExhausted as exc:
            # Never a failure: quota is a scheduling fact. The attempt is given
            # back so a busy afternoon cannot deadletter a day of work.
            queue.defer(con, job, exc.retry_after, str(exc))
            status("retrying", str(exc))
            bound.warning("job deferred", retry_after=exc.retry_after, reason=str(exc))
        except SecSyncIncomplete as exc:
            state = queue.fail(
                con,
                job,
                str(exc),
                terminal=exc.permanent,
                minimum_delay=exc.retry_after,
            )
            status(state, str(exc))
            bound.warning("SEC verification incomplete", state=state, retry_after=exc.retry_after)
        except Exception as exc:
            state = queue.fail(con, job, f"{type(exc).__name__}: {exc}")
            status(state, str(exc))
            bound.error("job failed", state=state, error=str(exc), exc_info=True)
        else:
            queue.complete(con, job.job_id)
            elapsed = asyncio.get_running_loop().time() - started
            status("done", detail, round(elapsed, 2))
            bound.info("job done", seconds=round(elapsed, 2), detail=detail)

    async def _maintenance(self, con: duckdb.DuckDBPyConnection) -> None:
        now = datetime.now(UTC).replace(tzinfo=None)
        if now - self._last_reclaim < RECLAIM_EVERY:
            return
        self._last_reclaim = now
        reclaimed = queue.reclaim_stale(con)
        purged = queue.purge_done(con, older_than=PURGE_DONE_AFTER)
        if reclaimed or purged:
            log.info("queue maintenance", reclaimed=reclaimed, purged=purged)

    async def _worker(self, con: duckdb.DuckDBPyConnection) -> None:
        while not self.stopping.is_set():
            async with self._db_lock:
                from finlab.engine.control import drain_requests

                drain_requests(con, self.settings.data_dir)
                await self._maintenance(con)
                job = queue.claim(con, job_types=list(HANDLERS))
            if job is None:
                # Sleep, but wake immediately on shutdown rather than making
                # SIGTERM wait out the idle interval.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stopping.wait(), timeout=IDLE_SLEEP_SECONDS)
                continue
            async with self._db_lock:
                await self._run_job(con, job)

    async def run(self) -> None:
        configure_logging(self.settings)
        self.settings.ensure_dirs()

        with write_conn(self.settings) as con:
            # A previous process may have been killed mid-job; those rows are
            # stranded in `running` and nothing else would ever move them.
            reclaimed = queue.reclaim_stale(con, older_than=timedelta(0))
            log.info(
                "finlabd starting",
                reclaimed=reclaimed,
                handlers=sorted(HANDLERS),
                queue=queue.counts(con),
            )

            unhandled = [s.job_type for s in SCHEDULE if s.job_type not in HANDLERS]
            if unhandled:
                log.info("schedule entries without a handler yet", job_types=unhandled)

            # A fresh in-process scheduler cannot replay triggers missed while
            # stopped. A watermark-based synchronization catches up once at boot.
            from finlab.ingest.calendar import last_completed_session

            target = last_completed_session(datetime.now(UTC)).isoformat()
            queue.enqueue(con, "prices", {"catch_up_through": target}, priority=50)

            scheduler = build_scheduler(self._enqueue_scheduled(con))
            for job_id in [s.job_type for s in SCHEDULE if s.job_type not in HANDLERS]:
                scheduler.remove_job(job_id)
            scheduler.start()

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.stopping.set)

            try:
                await self._worker(con)
            finally:
                scheduler.shutdown(wait=False)
                log.info("finlabd stopped", queue=queue.counts(con))


def main() -> None:
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()
