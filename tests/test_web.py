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


def _minimal_settings(tmp_path: Path) -> Settings:
    return Settings(
        raw={
            "app": {"name": "Test Agent", "database": "data/test.db", "export_directory": "exports"},
            "safety": {"publish_enabled": False, "require_approval": True, "allow_external_writes": False},
            "research": {},
            "scoring": {},
            "drafting": {"platforms": ["linkedin"], "max_pending_drafts": 10},
            "scheduler": {},
        },
        profile={},
        project_root=tmp_path,
    )


def test_dashboard_shows_research_item_count(tmp_path: Path):
    settings = _minimal_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_item(
        ResearchItem(
            id="item_count",
            title="Cloud AI",
            abstract="Evidence",
            url="https://example.com",
            source_name="Conference",
            source_type="peer_reviewed",
            content_hash="hash_count",
        )
    )
    page = TestClient(create_app(settings)).get("/")
    assert page.status_code == 200
    assert "built-in method" not in page.text
    assert "<span>1</span><small>research items</small>" in page.text


def test_run_research_returns_immediately_and_blocks_overlap(tmp_path: Path, monkeypatch):
    import threading

    from brand_agent import pipeline as pipeline_module

    release = threading.Event()
    started = threading.Event()
    calls = []

    def slow_run(self):
        calls.append(1)
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(pipeline_module.ResearchPipeline, "run", slow_run)
    client = TestClient(create_app(_minimal_settings(tmp_path)))

    first = client.post("/pipeline/run", follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/?research=started"
    assert started.wait(timeout=5)

    second = client.post("/pipeline/run", follow_redirects=False)
    assert second.headers["location"] == "/?research=already_running"
    assert "Research is running in the background" in client.get("/").text

    release.set()
    assert len(calls) == 1
