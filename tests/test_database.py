from pathlib import Path

import pytest

from brand_agent.config import Settings
from brand_agent.db import Database
from brand_agent.models import ContentDraft, ResearchBrief, ResearchItem
from brand_agent.publishing import ApprovalRequiredError, export_approved_draft


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        raw={
            "app": {"database": "data/test.db", "export_directory": "exports"},
            "safety": {
                "publish_enabled": False,
                "require_approval": True,
                "allow_external_writes": False,
            },
        },
        profile={},
        project_root=tmp_path,
    )


def seed(database: Database):
    item = ResearchItem(
        id="item_1",
        title="Cloud AI paper",
        abstract="An abstract about production AI infrastructure.",
        url="https://example.com/paper",
        source_name="Conference",
        source_type="peer_reviewed",
        content_hash="hash_1",
    )
    database.upsert_item(item)
    brief = ResearchBrief(
        id="brief_1",
        item_id=item.id,
        headline=item.title,
        problem="Problem",
        contribution="Contribution",
        evidence="Evidence",
        limitations="Limitations",
        cloud_implications="Cloud implications",
        industry_implications="Industry implications",
        founder_questions=["Question"],
        confidence=0.9,
    )
    database.save_brief(brief)
    draft = ContentDraft(
        id="draft_1",
        brief_id=brief.id,
        platform="linkedin",
        title="Cloud AI",
        body="Approved body",
        rationale="Relevant",
    )
    database.save_draft(draft)


def test_export_requires_approval(tmp_path: Path):
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    draft = database.get_draft("draft_1")
    assert draft is not None
    with pytest.raises(ApprovalRequiredError):
        export_approved_draft(database=database, settings=settings, draft=draft)


def test_approved_draft_exports_locally(tmp_path: Path):
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    seed(database)
    database.set_draft_status("draft_1", "approved")
    draft = database.get_draft("draft_1")
    assert draft is not None
    path = export_approved_draft(database=database, settings=settings, draft=draft)
    assert path.exists()
    assert "Approved body" in path.read_text(encoding="utf-8")
    assert database.get_draft("draft_1").status == "exported"

