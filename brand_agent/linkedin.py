from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import httpx

from .auth import LinkedInAuthError, LinkedInOAuth
from .cards import alt_text, card_from_json, render_card
from .config import Settings
from .db import Database
from .linkedin_format import to_linkedin_commentary
from .models import ContentDraft
from .publishing import assert_publish_allowed


class LinkedInPublishError(RuntimeError):
    pass


def _upload_card_image(headers: dict[str, str], owner: str, png: bytes) -> str:
    """Images API: initializeUpload -> PUT bytes -> image URN for content.media.id."""
    init = httpx.post(
        "https://api.linkedin.com/rest/images?action=initializeUpload",
        headers=headers,
        json={"initializeUploadRequest": {"owner": owner}},
        timeout=30,
    )
    if init.status_code not in {200, 201}:
        raise LinkedInPublishError(f"LinkedIn image upload init failed (HTTP {init.status_code}): {init.text[:300]}")
    try:
        value = init.json()["value"]
        upload_url, image_urn = value["uploadUrl"], value["image"]
    except (ValueError, KeyError, TypeError) as exc:
        raise LinkedInPublishError("LinkedIn image upload init returned an unexpected response") from exc
    put = httpx.put(
        upload_url,
        headers={"Authorization": headers["Authorization"], "Content-Type": "application/octet-stream"},
        content=png,
        timeout=60,
    )
    if put.status_code not in {200, 201}:
        raise LinkedInPublishError(f"LinkedIn image upload failed (HTTP {put.status_code})")
    time.sleep(3)  # member tokens cannot poll image status; give processing a head start
    return image_urn


def publish_linkedin_draft(
    *,
    database: Database,
    settings: Settings,
    draft: ContentDraft,
) -> str:
    if draft.platform != "linkedin":
        raise LinkedInPublishError("Automatic publishing is implemented only for LinkedIn")
    assert_publish_allowed(settings, draft)

    try:
        credential = LinkedInOAuth(settings, database).credential()
    except LinkedInAuthError as exc:
        raise LinkedInPublishError(str(exc)) from exc
    if not credential:
        raise LinkedInPublishError("Connect LinkedIn before publishing")
    try:
        if datetime.fromisoformat(credential["expires_at"]) <= datetime.now(timezone.utc):
            raise LinkedInPublishError("LinkedIn authorization expired; reconnect from the dashboard")
    except (KeyError, TypeError, ValueError) as exc:
        raise LinkedInPublishError("LinkedIn authorization has an invalid expiry value") from exc

    if not database.begin_publishing(draft.id):
        current = database.get_draft(draft.id)
        if current and current.status == "published" and current.external_post_id:
            return current.external_post_id
        raise LinkedInPublishError("This draft is not in a publishable state")

    version = os.getenv("LINKEDIN_API_VERSION", "202610")
    author = f"urn:li:person:{credential['subject']}"
    headers = {
        "Authorization": f"Bearer {credential['access_token']}",
        "LinkedIn-Version": version,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }
    payload = {
        "author": author,
        "commentary": to_linkedin_commentary(draft.body),
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    try:
        card = card_from_json(draft.card_json)
        if card:
            image_urn = _upload_card_image(headers, author, render_card(card))
            payload["content"] = {"media": {"id": image_urn, "altText": alt_text(card)}}
        for attempt in range(2):
            response = httpx.post("https://api.linkedin.com/rest/posts", headers=headers, json=payload, timeout=30)
            # A just-uploaded image can still be processing; give it one more moment.
            if card and attempt == 0 and response.status_code in {400, 422, 500, 503}:
                time.sleep(8)
                continue
            break
        if response.status_code not in {200, 201}:
            raise LinkedInPublishError(
                f"LinkedIn returned HTTP {response.status_code}: {response.text[:500]}"
            )
        post_id = response.headers.get("x-restli-id", "").strip()
        if not post_id:
            try:
                post_id = str(response.json().get("id", "")).strip()
            except ValueError:
                post_id = ""
        post_id = post_id or f"linkedin-post-{draft.id}"
        database.mark_publish_succeeded(draft.id, post_id)
        return post_id
    except Exception as exc:  # never leave a draft stuck in "publishing"
        message = str(exc) if isinstance(exc, LinkedInPublishError) else f"LinkedIn publish failed: {type(exc).__name__}"
        database.mark_publish_failed(draft.id, message)
        if isinstance(exc, LinkedInPublishError):
            raise
        raise LinkedInPublishError(message) from exc
