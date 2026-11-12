"""Posting approved drafts to the owner's X account (X API v2, OAuth 2.0 + PKCE).

Pay-per-use pricing makes a post with a URL far more expensive than one
without, so by default X drafts cite the paper (e.g. "arXiv:2609.33023")
instead of linking. Publishing refuses link-bearing text unless
X_INCLUDE_LINK=true, so the approved text is always exactly what is posted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from .auth import LinkedInAuthError, TokenCipher
from .cards import card_from_json, render_image
from .config import Settings
from .db import Database
from .models import ContentDraft
from .publishing import assert_publish_allowed

AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
API = "https://api.x.com/2"
SCOPES = "tweet.read tweet.write users.read media.write offline.access"
URL_PATTERN = re.compile(r"https?://\S+")
MAX_WEIGHTED_LENGTH = 280
URL_WEIGHT = 23  # X counts every link as 23 characters


class XError(RuntimeError):
    pass


def x_include_link(settings: Settings) -> bool:
    value = os.getenv("X_INCLUDE_LINK")
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(settings.raw.get("drafting", {}).get("x_include_link", False))


def weighted_length(text: str) -> int:
    return len(URL_PATTERN.sub("x" * URL_WEIGHT, text))


def x_text_problem(text: str, *, include_link: bool) -> str | None:
    """Why this text cannot be posted as-is, or None when it is fine."""
    if not text.strip():
        return "The X post is empty."
    if URL_PATTERN.search(text) and not include_link:
        return (
            "This X draft contains a link, which X bills at a much higher rate. "
            "Edit it to cite the source without a URL (e.g. arXiv:2609.33023), "
            "or set X_INCLUDE_LINK=true on Render."
        )
    if weighted_length(text) > MAX_WEIGHTED_LENGTH:
        return f"The X post is {weighted_length(text)} characters; the limit is {MAX_WEIGHTED_LENGTH}."
    return None


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


class XOAuth:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.client_id = os.getenv("X_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("X_CLIENT_SECRET", "").strip()
        try:
            self.cipher = TokenCipher(os.getenv("TOKEN_ENCRYPTION_KEY", ""))
        except LinkedInAuthError as exc:
            raise XError(str(exc)) from exc

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.base_url}/auth/x/callback"

    def _basic_auth(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}", "Content-Type": "application/x-www-form-urlencoded"}

    def authorization_url(self, *, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": SCOPES,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = httpx.post(TOKEN_URL, headers=self._basic_auth(), data=form, timeout=20)
        except httpx.HTTPError as exc:
            raise XError(f"X token request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise XError(f"X token request failed (HTTP {response.status_code}): {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise XError("X token response was not JSON") from exc

    def _save(self, tokens: dict[str, Any], *, subject: str, username: str, previous_refresh: str = "") -> None:
        refresh = str(tokens.get("refresh_token") or previous_refresh)
        if not refresh:
            raise XError("X did not return a refresh token; make sure the app requests offline.access")
        secret = json.dumps({"access_token": str(tokens["access_token"]), "refresh_token": refresh})
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(tokens.get("expires_in", 7200)))
        self.database.save_oauth_credential(
            provider="x",
            subject=subject,
            email="",
            display_name=username,
            access_token_encrypted=self.cipher.encrypt(secret),
            expires_at=expires_at.isoformat(),
            scope=str(tokens.get("scope", SCOPES)),
        )

    def complete(self, *, code: str, code_verifier: str) -> str:
        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": code_verifier,
            }
        )
        try:
            me = httpx.get(f"{API}/users/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=20)
            me.raise_for_status()
            user = me.json()["data"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise XError(f"Could not read the connected X account: {type(exc).__name__}") from exc
        username = f"@{user.get('username', '')}"
        self._save(tokens, subject=str(user["id"]), username=username)
        self.database.log_event("x_connected", "oauth", str(user["id"]), {"username": username})
        return username

    def connection(self) -> dict[str, Any] | None:
        stored = self.database.get_oauth_credential("x")
        return {"username": stored["display_name"], "subject": stored["subject"]} if stored else None

    def access_token(self) -> tuple[str, str]:
        """(access_token, user_id), refreshing first when it expires within 5 minutes."""
        stored = self.database.get_oauth_credential("x")
        if not stored:
            raise XError("Connect X on the dashboard before posting")
        try:
            secret = json.loads(self.cipher.decrypt(stored["access_token_encrypted"]))
        except (LinkedInAuthError, ValueError) as exc:
            raise XError("The stored X token cannot be read; reconnect X") from exc
        try:
            expires_at = datetime.fromisoformat(stored["expires_at"])
        except (TypeError, ValueError):
            expires_at = datetime.now(timezone.utc)
        if expires_at - datetime.now(timezone.utc) > timedelta(minutes=5):
            return secret["access_token"], stored["subject"]
        tokens = self._token_request({"grant_type": "refresh_token", "refresh_token": secret["refresh_token"]})
        self._save(tokens, subject=stored["subject"], username=stored["display_name"], previous_refresh=secret["refresh_token"])
        return str(tokens["access_token"]), stored["subject"]


def _upload_image(token: str, png: bytes) -> str:
    response = httpx.post(
        f"{API}/media/upload",
        headers={"Authorization": f"Bearer {token}"},
        json={"media": base64.b64encode(png).decode("ascii"), "media_category": "tweet_image"},
        timeout=60,
    )
    if response.status_code not in {200, 201}:
        raise XError(f"X image upload failed (HTTP {response.status_code}): {response.text[:300]}")
    try:
        return str(response.json()["data"]["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise XError("X image upload returned an unexpected response") from exc


def publish_x_draft(*, database: Database, settings: Settings, draft: ContentDraft) -> str:
    if draft.platform != "x":
        raise XError("Not an X draft")
    assert_publish_allowed(settings, draft)
    if not database.begin_publishing(draft.id):
        current = database.get_draft(draft.id)
        if current and current.status == "published" and current.external_post_id:
            return current.external_post_id
        raise XError("This draft is not in a publishable state")
    try:
        # Checked after begin_publishing so any failure is recorded on the draft and retryable.
        problem = x_text_problem(draft.body, include_link=x_include_link(settings))
        if problem:
            raise XError(problem)
        oauth = XOAuth(settings, database)
        if not oauth.configured:
            raise XError("X is not configured on the server (X_CLIENT_ID / X_CLIENT_SECRET)")
        token, _user_id = oauth.access_token()
        payload: dict[str, Any] = {"text": draft.body}
        sibling = database.get_draft_for_brief(draft.brief_id, "linkedin")
        image = card_from_json(draft.card_json) or (card_from_json(sibling.card_json) if sibling else None)
        if image:
            payload["media"] = {"media_ids": [_upload_image(token, render_image(image))]}
        response = httpx.post(
            f"{API}/tweets",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if response.status_code not in {200, 201}:
            raise XError(f"X returned HTTP {response.status_code}: {response.text[:400]}")
        post_id = str(response.json()["data"]["id"])
        database.mark_publish_succeeded(draft.id, post_id)
        return post_id
    except Exception as exc:  # never leave a draft stuck in "publishing"
        message = str(exc) if isinstance(exc, XError) else f"X publish failed: {type(exc).__name__}"
        database.mark_publish_failed(draft.id, message)
        raise XError(message) from exc
