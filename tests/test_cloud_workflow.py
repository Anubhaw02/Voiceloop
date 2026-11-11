from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from brand_agent.auth import TokenCipher
from brand_agent.config import Settings
from brand_agent.db import Database
from brand_agent.linkedin import publish_linkedin_draft
from brand_agent.models import ContentDraft, ResearchBrief, ResearchItem
from brand_agent.notifier import send_pending_review_reminder


def make_settings(tmp_path: Path, *, publishing: bool = False) -> Settings:
    return Settings(
        raw={
            "app": {
                "name": "Test Agent",
                "database": "data/test.db",
                "export_directory": "exports",
            },
            "safety": {
                "publish_enabled": publishing,
                "require_approval": True,
                "allow_external_writes": publishing,
            },
            "research": {},
            "scoring": {},
            "drafting": {"platforms": ["linkedin"]},
            "scheduler": {},
        },
        profile={},
        project_root=tmp_path,
    )


def seed_pending(database: Database) -> ContentDraft:
    item = ResearchItem(
        id="cloud_item",
        title="Cloud AI",
        abstract="Evidence",
        url="https://example.com",
        source_name="Conference",
        source_type="peer_reviewed",
        content_hash="cloud_hash",
    )
    database.upsert_item(item)
    brief = ResearchBrief(
        id="cloud_brief",
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
    draft = ContentDraft(
        id="cloud_draft",
        brief_id=brief.id,
        platform="linkedin",
        title="Draft",
        body="Exact approved text",
        rationale="Relevant",
    )
    database.save_draft(draft)
    return draft


def test_linkedin_publish_is_idempotent(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path, publishing=True)
    database = Database(settings.database_url)
    database.initialize()
    draft = seed_pending(database)
    database.set_draft_status(draft.id, "approved")
    approved = database.get_draft(draft.id)
    assert approved is not None

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key)
    monkeypatch.setenv("LINKEDIN_CLIENT_ID", "client")
    monkeypatch.setenv("LINKEDIN_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")
    database.save_oauth_credential(
        provider="linkedin",
        subject="member-123",
        email="owner@example.com",
        display_name="Owner",
        access_token_encrypted=TokenCipher(key).encrypt("access-token"),
        expires_at=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        scope="openid profile email w_member_social",
    )

    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return httpx.Response(
            201,
            headers={"x-restli-id": "urn:li:share:123"},
            request=httpx.Request("POST", "https://api.linkedin.com/rest/posts"),
        )

    monkeypatch.setattr("brand_agent.linkedin.httpx.post", fake_post)
    first = publish_linkedin_draft(database=database, settings=settings, draft=approved)
    second = publish_linkedin_draft(database=database, settings=settings, draft=approved)

    assert first == second == "urn:li:share:123"
    assert len(calls) == 1
    assert database.get_draft(draft.id).status == "published"


def test_review_reminder_is_sent_once_per_day(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path)
    database = Database(settings.database_url)
    database.initialize()
    seed_pending(database)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("REMINDER_EMAIL", "owner@example.com")

    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return httpx.Response(
            200,
            json={"id": "email-1"},
            request=httpx.Request("POST", "https://api.resend.com/emails"),
        )

    monkeypatch.setattr("brand_agent.notifier.httpx.post", fake_post)
    first = send_pending_review_reminder(settings=settings, database=database)
    second = send_pending_review_reminder(settings=settings, database=database)

    assert first["status"] == "sent"
    assert second["status"] == "already_sent_recently"
    assert len(calls) == 1
