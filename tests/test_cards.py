import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from PIL import Image

from brand_agent.ai_research import _with_source, ai_brief, ai_drafts, build_card
from brand_agent.auth import TokenCipher
from brand_agent.cards import alt_text, card_from_json, render_card
from brand_agent.db import Database
from brand_agent.linkedin import publish_linkedin_draft
from brand_agent.llm import LLMRouter
from tests.test_ai_research import ABSTRACT, GOOD_BRIEF, LINKEDIN, FakeClient, make_item
from tests.test_cloud_workflow import make_settings, seed_pending

CARD = {
    "enabled": True,
    "topic": "CLOUD FOR AI",
    "headline": "Sharing KV caches beats buying more GPUs",
    "stat": "37%",
    "stat_label": "lower p99 latency vs vLLM",
    "source": "KVShare (arXiv)",
    "status_note": "Preprint · not yet peer reviewed",
    "author": "Anubhaw · Cloud × AI research",
}


def test_card_renders_portrait_png():
    image = Image.open(io.BytesIO(render_card(CARD)))
    assert image.format == "PNG" and image.size == (1080, 1350)
    no_stat = dict(CARD, stat="", stat_label="")
    assert Image.open(io.BytesIO(render_card(no_stat))).size == (1080, 1350)


def test_build_card_drops_numbers_not_in_source():
    item = make_item()
    good = build_card({"headline": "Cache sharing cuts tail latency", "stat": "37%", "stat_label": "lower p99"}, item, {}, ABSTRACT)
    assert good["stat"] == "37%" and good["status_note"].startswith("Preprint")
    invented = build_card({"headline": "Cache sharing cuts tail latency", "stat": "80%", "stat_label": "lower p99"}, item, {}, ABSTRACT)
    assert invented["stat"] == "" and invented["headline"]
    assert build_card({"headline": "Cuts cost by 90% for everyone", "stat": ""}, item, {}, ABSTRACT) is None
    assert build_card("not a dict", item, {}, ABSTRACT) is None


def test_card_toggle_and_alt_text():
    assert card_from_json(json.dumps(CARD))["stat"] == "37%"
    assert card_from_json(json.dumps(dict(CARD, enabled=False))) is None
    assert card_from_json(None) is None and card_from_json("{bad") is None
    assert "37%" in alt_text(CARD) and "Source: KVShare" in alt_text(CARD)


def test_source_link_goes_above_hashtags():
    assert _with_source("Body text\n\n#SRE #Cloud", "https://x.org/1") == "Body text\n\nSource: https://x.org/1\n\n#SRE #Cloud"
    assert _with_source("Body text", "https://x.org/1") == "Body text\n\nSource: https://x.org/1"


def test_cliche_draft_is_rewritten():
    item = make_item()
    brief, claims = ai_brief(LLMRouter([FakeClient([GOOD_BRIEF])]), item, ABSTRACT, "full_text")
    cliche = {"linkedin": LINKEDIN + "\n\nThis is a game-changer in today's landscape.", "x": "KVShare: 37% lower p99 latency vs vLLM."}
    clean = {"linkedin": LINKEDIN, "x": "KVShare: 37% lower p99 latency vs vLLM."}
    client = FakeClient([cliche, clean])
    result = ai_drafts(LLMRouter([client]), item, brief, claims, {}, ABSTRACT, platforms=["linkedin", "x"])
    assert "game-changer" not in result.bodies["linkedin"]
    assert "cliche phrases" in client.prompts[1]


def _connect_linkedin(database: Database, monkeypatch) -> None:
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


def test_publish_uploads_card_image_then_posts_with_media(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path, publishing=True)
    database = Database(settings.database_url)
    database.initialize()
    draft = seed_pending(database)
    assert database.set_draft_card_json(draft.id, json.dumps(CARD))
    database.set_draft_status(draft.id, "approved")
    _connect_linkedin(database, monkeypatch)
    monkeypatch.setattr("brand_agent.linkedin.time.sleep", lambda seconds: None)

    posts, puts = [], []

    def fake_post(url, **kwargs):
        posts.append((url, kwargs.get("json")))
        request = httpx.Request("POST", url)
        if "initializeUpload" in url:
            return httpx.Response(200, json={"value": {"uploadUrl": "https://upload.example/x", "image": "urn:li:image:42"}}, request=request)
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:9"}, request=request)

    def fake_put(url, **kwargs):
        puts.append((url, len(kwargs["content"])))
        return httpx.Response(201, request=httpx.Request("PUT", url))

    monkeypatch.setattr("brand_agent.linkedin.httpx.post", fake_post)
    monkeypatch.setattr("brand_agent.linkedin.httpx.put", fake_put)

    assert publish_linkedin_draft(database=database, settings=settings, draft=database.get_draft(draft.id)) == "urn:li:share:9"
    assert posts[0][1] == {"initializeUploadRequest": {"owner": "urn:li:person:member-123"}}
    assert puts and puts[0][0] == "https://upload.example/x" and puts[0][1] > 10_000
    media = posts[1][1]["content"]["media"]
    assert media["id"] == "urn:li:image:42" and "37%" in media["altText"]


def test_failed_image_upload_marks_draft_failed_not_stuck(tmp_path: Path, monkeypatch):
    settings = make_settings(tmp_path, publishing=True)
    database = Database(settings.database_url)
    database.initialize()
    draft = seed_pending(database)
    database.set_draft_card_json(draft.id, json.dumps(CARD))
    database.set_draft_status(draft.id, "approved")
    _connect_linkedin(database, monkeypatch)
    monkeypatch.setattr(
        "brand_agent.linkedin.httpx.post",
        lambda url, **kwargs: httpx.Response(403, text="forbidden", request=httpx.Request("POST", url)),
    )
    try:
        publish_linkedin_draft(database=database, settings=settings, draft=database.get_draft(draft.id))
    except Exception:
        pass
    current = database.get_draft(draft.id)
    assert current.status == "publish_failed" and "image upload init failed" in current.publish_error


def test_dashboard_shows_image_and_can_turn_it_off(tmp_path: Path):
    from fastapi.testclient import TestClient

    from brand_agent.web import create_app
    from tests.test_web import _minimal_settings

    settings = _minimal_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    draft = seed_pending(database)
    database.set_draft_card_json(draft.id, json.dumps(CARD))
    client = TestClient(create_app(settings))

    page = client.get(f"/drafts/{draft.id}")
    assert page.status_code == 200 and f"/drafts/{draft.id}/card.png" in page.text
    image = client.get(f"/drafts/{draft.id}/card.png")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"

    client.post(f"/drafts/{draft.id}/card", data={"enabled": "false"}, follow_redirects=False)
    assert card_from_json(database.get_draft(draft.id).card_json) is None
    assert client.get(f"/drafts/{draft.id}/card.png").status_code == 404
    assert "Image turned off" in client.get(f"/drafts/{draft.id}").text
