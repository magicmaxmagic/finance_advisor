"""Weekly research scheduling belongs to Analytics, never the warehouse writer."""

import asyncio
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from finlab.config import get_settings
from finlab.research.weekly_report import generate


def weekly_trigger() -> CronTrigger:
    return CronTrigger(day_of_week="sat", hour=12, minute=0, timezone="UTC")


async def run() -> None:
    stopping = asyncio.Event()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        generate,
        trigger=weekly_trigger(),
        args=[get_settings()],
        coalesce=True,
        max_instances=1,
        misfire_grace_time=86400,
    )
    scheduler.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)
    try:
        await stopping.wait()
    finally:
        scheduler.shutdown(wait=False)


def main() -> None:
    asyncio.run(run())
