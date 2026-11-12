import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from brand_agent.ai_research import x_source_citation
from brand_agent.db import Database
from brand_agent.models import ContentDraft
from brand_agent.x_client import XError, XOAuth, pkce_pair, publish_x_draft, weighted_length, x_text_problem
from tests.test_ai_research import make_item
from tests.test_cards import CARD
from tests.test_cloud_workflow import make_settings, seed_pending


@pytest.fixture
def x_env(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("X_CLIENT_ID", "x-client")
    monkeypatch.setenv("X_CLIENT_SECRET", "x-secret")
    monkeypatch.delenv("X_INCLUDE_LINK", raising=False)
    monkeypatch.setattr("brand_agent.x_client.httpx.post", None)  # every test must mock its own calls
    monkeypatch.setattr("brand_agent.x_client.httpx.get", None)


def _response(status, payload=None, url="https://api.x.com/2"):
    return httpx.Response(status, json=payload or {}, request=httpx.Request("POST", url))


def test_pkce_and_text_rules():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected

    assert x_text_problem("Agents fail at repair. arXiv:2609.33023", include_link=False) is None
    assert "contains a link" in x_text_problem("See https://arxiv.org/abs/1", include_link=False)
    assert x_text_problem("See https://arxiv.org/abs/1", include_link=True) is None
    assert weighted_length("a https://example.com/very/long/path/that/is/long") == 2 + 23
    assert "limit is 280" in x_text_problem("x" * 281, include_link=False)
    assert x_text_problem("   ", include_link=False) == "The X post is empty."


def test_citation_without_links():
    assert x_source_citation(make_item("item_cite")) == "arXiv:2611.00009"
    blog = make_item("item_blog")
    blog.url, blog.source_name, blog.raw = "https://cloud.google.com/blog/x", "Google Cloud Blog", {}
    assert x_source_citation(blog) == "Source: Google Cloud Blog"


def _connected(tmp_path: Path, x_env, monkeypatch, *, expires_in_seconds=7200):
    settings = make_settings(tmp_path, publishing=True)
    database = Database(settings.database_url)
    database.initialize()
    token_calls = []

    def fake_post(url, **kwargs):
        token_calls.append((url, kwargs))
        return _response(200, {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": expires_in_seconds, "scope": "tweet.write"})

    monkeypatch.setattr("brand_agent.x_client.httpx.post", fake_post)
    monkeypatch.setattr(
        "brand_agent.x_client.httpx.get",
        lambda url, **kwargs: _response(200, {"data": {"id": "42", "username": "anubhaw"}}, url),
    )
    username = XOAuth(settings, database).complete(code="code-1", code_verifier="verifier-1")
    return settings, database, token_calls, username


def test_connect_stores_encrypted_tokens_with_basic_auth(tmp_path, x_env, monkeypatch):
    settings, database, token_calls, username = _connected(tmp_path, x_env, monkeypatch)
    assert username == "@anubhaw"
    url, kwargs = token_calls[0]
    assert url == "https://api.x.com/2/oauth2/token"
    assert kwargs["headers"]["Authorization"] == "Basic " + base64.b64encode(b"x-client:x-secret").decode()
    assert kwargs["data"] == {
        "grant_type": "authorization_code",
        "code": "code-1",
        "redirect_uri": f"{settings.base_url}/auth/x/callback",
        "code_verifier": "verifier-1",
    }
    stored = database.get_oauth_credential("x")
    assert stored["subject"] == "42" and stored["display_name"] == "@anubhaw"
    assert "refresh-1" not in stored["access_token_encrypted"]  # encrypted at rest
    assert XOAuth(settings, database).access_token() == ("access-1", "42")


def test_expiring_token_is_refreshed_and_rotation_saved(tmp_path, x_env, monkeypatch):
    settings, database, _calls, _ = _connected(tmp_path, x_env, monkeypatch, expires_in_seconds=60)
    refreshes = []

    def fake_refresh(url, **kwargs):
        refreshes.append(kwargs["data"])
        return _response(200, {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 7200})

    monkeypatch.setattr("brand_agent.x_client.httpx.post", fake_refresh)
    oauth = XOAuth(settings, database)
    assert oauth.access_token() == ("access-2", "42")
    assert refreshes == [{"grant_type": "refresh_token", "refresh_token": "refresh-1"}]
    assert oauth.access_token() == ("access-2", "42")  # fresh now: no second refresh
    assert len(refreshes) == 1
    secret = json.loads(oauth.cipher.decrypt(database.get_oauth_credential("x")["access_token_encrypted"]))
    assert secret["refresh_token"] == "refresh-2"


def _x_draft(database: Database, body: str) -> ContentDraft:
    linkedin = seed_pending(database)
    database.set_draft_card_json(linkedin.id, json.dumps(CARD))
    draft = ContentDraft(id="x_draft", brief_id=linkedin.brief_id, platform="x", title="X", body=body, rationale="r")
    database.save_draft(draft)
    database.set_draft_status(draft.id, "approved")
    return database.get_draft(draft.id)


def test_publish_posts_tweet_with_twin_image_once(tmp_path, x_env, monkeypatch):
    settings, database, _calls, _ = _connected(tmp_path, x_env, monkeypatch)
    draft = _x_draft(database, "Agents can diagnose but rarely repair. arXiv:2609.33023")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs.get("json")))
        if url.endswith("/media/upload"):
            return _response(200, {"data": {"id": "999", "media_key": "3_999"}}, url)
        return _response(201, {"data": {"id": "1888", "text": "..."}}, url)

    monkeypatch.setattr("brand_agent.x_client.httpx.post", fake_post)
    assert publish_x_draft(database=database, settings=settings, draft=draft) == "1888"
    upload, tweet = calls
    assert upload[1]["media_category"] == "tweet_image" and len(base64.b64decode(upload[1]["media"])) > 10_000
    assert tweet == ("https://api.x.com/2/tweets", {"text": draft.body, "media": {"media_ids": ["999"]}})
    assert database.get_draft(draft.id).status == "published"

    assert publish_x_draft(database=database, settings=settings, draft=draft) == "1888"
    assert len(calls) == 2  # no duplicate tweet


def test_link_bearing_draft_is_blocked_and_recorded(tmp_path, x_env, monkeypatch):
    settings, database, _calls, _ = _connected(tmp_path, x_env, monkeypatch)
    draft = _x_draft(database, "Read it: https://arxiv.org/abs/2609.33023")
    monkeypatch.setattr("brand_agent.x_client.httpx.post", lambda *a, **k: pytest.fail("must not call X"))
    with pytest.raises(XError, match="contains a link"):
        publish_x_draft(database=database, settings=settings, draft=draft)
    current = database.get_draft(draft.id)
    assert current.status == "publish_failed" and "contains a link" in current.publish_error


def test_dashboard_connect_flow(tmp_path, x_env, monkeypatch):
    from fastapi.testclient import TestClient

    from brand_agent.web import create_app
    from tests.test_web import _minimal_settings

    client = TestClient(create_app(_minimal_settings(tmp_path)))
    assert "Connect X" in client.get("/").text

    login = client.get("/auth/x/login", follow_redirects=False)
    assert login.status_code == 303
    location = urlparse(login.headers["location"])
    query = parse_qs(location.query)
    assert f"{location.scheme}://{location.netloc}{location.path}" == "https://x.com/i/oauth2/authorize"
    assert query["code_challenge_method"] == ["S256"] and query["client_id"] == ["x-client"]
    assert set(query["scope"][0].split()) == {"tweet.read", "tweet.write", "users.read", "media.write", "offline.access"}

    bad = client.get("/auth/x/callback", params={"code": "c", "state": "wrong"}, follow_redirects=False)
    assert bad.status_code == 400
