from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.blocking import BlockingScheduler

from .config import Settings
from .pipeline import ResearchPipeline


LOGGER = logging.getLogger(__name__)


def run_scheduler(settings: Settings) -> None:
    config = settings.raw["scheduler"]
    interval_hours = int(config.get("interval_hours", 12))
    lock = threading.Lock()

    def safe_run() -> None:
        if not lock.acquire(blocking=False):
            LOGGER.warning("Skipping cycle because the previous run is still active")
            return
        try:
            report = ResearchPipeline(settings).run()
            LOGGER.info(
                "Pipeline %s: %s, discovered=%s, briefs=%s, drafts=%s",
                report.run_id,
                report.status,
                report.discovered,
                report.briefs,
                report.drafts,
            )
        finally:
            lock.release()

    scheduler = BlockingScheduler(timezone=settings.raw["app"].get("timezone", "Asia/Kolkata"))
    scheduler.add_job(
        safe_run,
        "interval",
        hours=interval_hours,
        id="frontier_brand_research",
        max_instances=1,
        coalesce=True,
    )
    if config.get("run_on_start", True):
        safe_run()
    LOGGER.info("Scheduler running every %s hours", interval_hours)
    scheduler.start()

