from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Settings
from .db import Database
from .models import ContentDraft


class PublishingDisabledError(RuntimeError):
    pass


class ApprovalRequiredError(RuntimeError):
    pass


def assert_publish_allowed(settings: Settings, draft: ContentDraft) -> None:
    if not settings.publish_enabled:
        raise PublishingDisabledError(
            "Publishing is hard-disabled in config/agent.yaml. The local pipeline may only export approved drafts."
        )
    if settings.require_approval and draft.status not in {"approved", "publish_failed"}:
        raise ApprovalRequiredError("A human must approve this exact draft before publishing.")
    if not settings.allow_external_writes:
        raise PublishingDisabledError("External writes are disabled by the local safety policy.")


def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:80] or "draft"


def export_approved_draft(
    *,
    database: Database,
    settings: Settings,
    draft: ContentDraft,
) -> Path:
    if draft.status not in {"approved", "exported", "published"}:
        raise ApprovalRequiredError("Only approved drafts can be exported.")

    settings.export_directory.mkdir(parents=True, exist_ok=True)
    destination = settings.export_directory / f"{draft.platform}-{_slug(draft.title)}-{draft.id[-8:]}.md"
    metadata = {
        "draft_id": draft.id,
        "platform": draft.platform,
        "status": "approved_for_manual_publish",
        "risk_level": draft.risk_level,
    }
    content = f"---\n{json.dumps(metadata, indent=2)}\n---\n\n{draft.body}\n"
    destination.write_text(content, encoding="utf-8")
    if draft.status != "published":
        database.set_draft_status(draft.id, "exported")
    database.log_event("draft_exported", "draft", draft.id, {"path": str(destination)})
    return destination

