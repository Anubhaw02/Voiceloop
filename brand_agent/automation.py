from __future__ import annotations

from dataclasses import asdict

from .config import Settings
from .db import Database
from .notifier import send_pending_review_reminder
from .pipeline import ResearchPipeline


def run_daily(settings: Settings, database: Database) -> dict[str, object]:
    report = ResearchPipeline(settings, database).run()
    reminder = send_pending_review_reminder(settings=settings, database=database)
    return {"pipeline": asdict(report), "reminder": reminder}
