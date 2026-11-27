from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from fastapi.testclient import TestClient

from brand_agent.config import Settings
from brand_agent.db import Database
from brand_agent.models import ContentDraft, ResearchBrief, ResearchItem, ResearchMission
from brand_agent.web import create_app
from brand_agent.workspace import next_scheduled_slot


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
    review_page = client.get("/drafts/draft_web")
    assert review_page.status_code == 200
    assert "WHY THIS FITS YOU" in review_page.text
    assert "Approve with judgment" in review_page.text
    assert "Approve exact text" in review_page.text

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
    assert "<span>1</span><small>signals in research library</small>" in page.text
    assert "Research is a funnel, not a post counter" in page.text
    assert page.text.index("Posts ready for review") < page.text.index("Research is a funnel, not a post counter")


def test_dashboard_separates_actionable_and_published_drafts(tmp_path: Path):
    settings = _minimal_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    item = ResearchItem(
        id="item_queue",
        title="Queue research",
        abstract="Evidence",
        url="https://example.com/queue",
        source_name="Conference",
        source_type="peer_reviewed",
        content_hash="hash_queue",
    )
    database.upsert_item(item)
    brief = ResearchBrief(
        id="brief_queue",
        item_id=item.id,
        headline=item.title,
        problem="Problem",
        contribution="Contribution",
        evidence="Evidence",
        limitations="Limitations",
        cloud_implications="Cloud",
        industry_implications="Industry",
        founder_questions=["Question"],
        confidence=0.9,
    )
    database.save_brief(brief)
    database.save_draft(
        ContentDraft(
            id="draft_published",
            brief_id=brief.id,
            platform="linkedin",
            title="Published history title",
            body="Published body",
            rationale="Test",
        )
    )
    client = TestClient(create_app(settings))
    assert client.post("/drafts/draft_published/feedback", data={"comments": 2}).status_code == 409
    assert database.approve_draft_exact("draft_published", "Published body")
    assert database.begin_publishing("draft_published")
    database.mark_publish_succeeded("draft_published", "urn:li:share:123")

    page = client.get("/")

    assert page.status_code == 200
    assert page.text.count("Published history title") == 1
    assert "Recently published" in page.text
    assert "No LinkedIn draft needs action" in page.text
    assert "Published history title" not in client.get("/drafts").text
    assert "Published history title" in client.get("/drafts?view=published&platform=linkedin").text

    brand = client.get("/brand")
    assert brand.status_code == 200
    assert "1 published post" in brand.text
    assert "1 published</span>" in brand.text
    assert client.post("/drafts/draft_published/feedback", data={"comments": -1}).status_code == 422
    feedback = client.post(
        "/drafts/draft_published/feedback",
        data={"impressions": "125", "comments": "", "conversations": "2", "note": "Two useful replies."},
    )
    assert feedback.status_code == 200
    assert "Feedback saved." in feedback.text
    assert "Two useful replies." in feedback.text
    record = database.post_feedback("draft_published")
    assert record["impressions"] == 125 and record["comments"] is None
    assert database.get_draft("draft_published").status == "published"
    assert "Two useful replies." not in client.get("/drafts?view=published").text


def test_dashboard_surfaces_latest_run_warnings(tmp_path: Path):
    settings = _minimal_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    database.start_run("run_warning")
    database.finish_run(
        "run_warning",
        status="completed_with_warnings",
        discovered_count=20,
        new_item_count=2,
        brief_count=1,
        draft_count=1,
        errors=["AI fallback used after provider overload"],
        summary={
            "drafts_by_platform": {"linkedin": 1, "x": 0},
            "full_queues_at_start": ["x"], "repeated_results": 18, "quarantined_results": 0,
            "below_score_threshold": 3, "already_covered": 2, "candidates_reviewed": 3,
            "topics_selected": 1, "ai_skipped": 1,
        },
    )

    page = TestClient(create_app(settings)).get("/")

    assert page.status_code == 200
    assert "1 warning" in page.text
    assert "AI fallback used after provider overload" in page.text
    assert "20 scanned · 2 new · 1 brief · 1 draft" in page.text
    assert "LinkedIn 1 · X 0" in page.text
    assert "Full at start: X" in page.text
    assert "18 source results were already in the library" in page.text


def test_research_search_paginates_and_excludes_quarantined_items(tmp_path: Path):
    settings = _minimal_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    for index in range(18):
        database.upsert_item(ResearchItem(
            id=f"item_{index:02d}", title=f"Cloud Bench {index:02d}", abstract="100% coverage",
            url=f"https://example.com/{index}", source_name="Conference",
            source_type="peer_reviewed" if index < 17 else "other",
            content_hash=f"hash_{index}", topic="cloud_for_ai",
            status="quarantined" if index == 16 else "scored",
        ))
    client = TestClient(create_app(settings))
    url = "/research?q=100%25&topic=cloud_for_ai&source=peer_reviewed"
    first = client.get(url)
    assert first.status_code == 200
    assert "Cloud Bench 00" in first.text and "Cloud Bench 15" not in first.text
    assert "q=100%25" in first.text and "page=2" in first.text
    second = client.get(url + "&page=2")
    assert "Cloud Bench 15" in second.text
    assert "Cloud Bench 16" not in second.text and "Cloud Bench 17" not in second.text


def test_mission_flow_keeps_completed_reflection_private(tmp_path: Path):
    settings = _minimal_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    database.upsert_item(ResearchItem(
        id="mission_item", title="Cloud Lab", abstract="Evidence", url="https://example.com/m",
        source_name="Conference", source_type="peer_reviewed", content_hash="mission_hash",
    ))
    database.save_mission(ResearchMission(
        id="mission_a", item_id="mission_item", title="Compare approaches",
        objective="Run a benchmark", evidence_required="A chart",
    ))
    client = TestClient(create_app(settings))
    assert "Start mission" in client.get("/missions?status=proposed").text
    assert "Complete with evidence" in client.post("/missions/mission_a/activate").text
    reflection = "The second approach reduces latency in the test environment."
    assert client.post("/missions/mission_a/complete", data={"reflection": reflection}).status_code == 200
    done = client.get("/missions?status=completed")
    assert reflection in done.text
    assert "Complete with evidence" not in done.text
    assert reflection not in client.get("/").text


def test_workspace_pages_require_auth_when_enabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    client = TestClient(create_app(_minimal_settings(tmp_path)))
    for path in ("/drafts", "/research", "/missions", "/brand"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/auth/linkedin/login"
    assert client.post("/drafts/post/feedback", follow_redirects=False).status_code == 303


def test_configured_run_slots_match_cloud_workflow():
    root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((root / "config/agent.yaml").read_text(encoding="utf-8"))
    workflow = yaml.load((root / ".github/workflows/daily-agent.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    entries = workflow["on"]["schedule"]
    times = config["scheduler"]["cloud_daily_times"]
    assert {entry["timezone"] for entry in entries} == {config["app"]["timezone"]}
    assert {entry["cron"] for entry in entries} == {
        f"{int(minute)} {int(hour)} * * *" for hour, minute in (slot.split(":") for slot in times)
    }
    now = datetime(2026, 11, 27, 14, 17, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert next_scheduled_slot(now, times) == now.replace(day=28, hour=8)


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
