"""APScheduler cron triggers per the architecture schedule table.

The scheduler does not do work. It only *enqueues* — every trigger writes a job
row and returns immediately, so a slow ingest can never block the next trigger,
and a daemon restart mid-task loses nothing because the job is already durable.

Schedule (architecture §8):

    06:00  universe snapshot, prices, EDGAR daily index
    07:00  new filings -> fetch -> section split -> enqueue extractions
    08:00  L2/L3 rebuild for affected CIKs, full-universe screen
    weekly full companyfacts refresh, concept coverage check

The universe snapshot is deliberately the first thing scheduled and the last
thing anyone should disable: it is the only job whose missed runs cannot be
recovered by running it again later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from finlab.daemon.queue import enqueue

# Lower runs first.
PRIORITY_UNIVERSE = 10  # irreplaceable if missed
PRIORITY_INGEST = 50
PRIORITY_DERIVE = 100
PRIORITY_MAINTENANCE = 200


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    job_type: str
    cron: str
    priority: int
    description: str


SCHEDULE: tuple[ScheduledJob, ...] = (
    ScheduledJob(
        "universe_snapshot",
        "30 18 * * mon-fri",
        PRIORITY_UNIVERSE,
        "capture the symbol directory — cannot be backfilled",
    ),
    ScheduledJob("prices", "0 6 * * mon-fri", PRIORITY_INGEST, "daily OHLCV"),
    ScheduledJob("edgar_index", "0 7 * * *", PRIORITY_INGEST, "discover new filings"),
    ScheduledJob("submissions", "30 7 * * *", PRIORITY_INGEST, "acceptance times"),
    ScheduledJob("rebuild_derived", "0 8 * * *", PRIORITY_DERIVE, "concept map, Q4, metrics"),
    ScheduledJob("companyfacts_refresh", "0 3 * * sun", PRIORITY_MAINTENANCE, "weekly refresh"),
    ScheduledJob("coverage_check", "0 4 * * sun", PRIORITY_MAINTENANCE, "concept coverage"),
)


def build_scheduler(enqueue_fn: Callable[[str, int], None]) -> AsyncIOScheduler:
    """Wire the schedule to an enqueue callback. Nothing here touches the network."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    for entry in SCHEDULE:
        minute, hour, day, month, day_of_week = entry.cron.split()
        scheduler.add_job(
            enqueue_fn,
            trigger=CronTrigger(
                minute=minute, hour=hour, day=day, month=month, day_of_week=day_of_week
            ),
            args=[entry.job_type, entry.priority],
            id=entry.job_type,
            name=entry.description,
            replace_existing=True,
            # A daemon that was down over several triggers should run the job
            # once on return, not once per missed trigger.
            coalesce=True,
            misfire_grace_time=3600,
            max_instances=1,
        )
    return scheduler


def enqueue_scheduled(con, job_type: str, priority: int) -> str:
    """Enqueue a scheduled job. Idempotent by job_id, so a double trigger is one row."""
    return enqueue(con, job_type, {"scheduled": True}, priority=priority)
