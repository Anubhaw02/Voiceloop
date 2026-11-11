from pathlib import Path

from fastapi.testclient import TestClient

from brand_agent.config import Settings
from brand_agent.db import Database
from brand_agent.models import ContentDraft, ResearchBrief, ResearchItem
from brand_agent.web import create_app


def test_approval_requires_integrity_checklist(tmp_path: Path):
    settings = Settings(
        raw={
            "app": {
                "name": "Test Agent",
                "database": "data/test.db",
                "export_directory": "exports",
            },
            "safety": {
                "publish_enabled": False,
                "require_approval": True,
                "allow_external_writes": False,
            },
            "research": {},
            "scoring": {},
            "drafting": {"platforms": ["linkedin"]},
            "scheduler": {},
        },
        profile={},
        project_root=tmp_path,
    )
    database = Database(settings.database_path)
    database.initialize()
    item = ResearchItem(
        id="item_web",
        title="Cloud AI",
        abstract="Evidence",
        url="https://example.com",
        source_name="Conference",
        source_type="peer_reviewed",
        content_hash="hash_web",
    )
    database.upsert_item(item)
    brief = ResearchBrief(
        id="brief_web",
        item_id=item.id,
        headline=item.title,
        problem="Problem",
        contribution="Contribution",
        evidence="Evidence",
        limitations="Limitations",
        cloud_implications="Implications",
        industry_implications="Industry",
        founder_questions=["Question"],
        confidence=0.9,
    )
    database.save_brief(brief)
    database.save_draft(
        ContentDraft(
            id="draft_web",
            brief_id=brief.id,
            platform="linkedin",
            title="Test draft",
            body="Exact text",
            rationale="Test",
        )
    )

    client = TestClient(create_app(settings))
    incomplete = client.post(
        "/drafts/draft_web/approve",
        data={"body": "Exact text"},
        follow_redirects=False,
    )
    assert incomplete.status_code == 400
    assert database.get_draft("draft_web").status == "pending_approval"

    complete = client.post(
        "/drafts/draft_web/approve",
        data={
            "body": "Exact text",
            "understood": "true",
            "checked_claims": "true",
            "represents_me": "true",
        },
        follow_redirects=False,
    )
    assert complete.status_code == 303
    assert database.get_draft("draft_web").status == "approved"

    replay = client.post(
        "/drafts/draft_web/approve",
        data={
            "body": "A replay must not replace approved text",
            "understood": "true",
            "checked_claims": "true",
            "represents_me": "true",
        },
        follow_redirects=False,
    )
    assert replay.status_code == 303
    approved = database.get_draft("draft_web")
    assert approved is not None
    assert approved.body == "Exact text"

