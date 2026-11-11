from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from .auth import LinkedInAuthError, LinkedInOAuth
from .config import Settings
from .db import Database
from .models import ContentDraft
from .publishing import assert_publish_allowed


class LinkedInPublishError(RuntimeError):
    pass


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
    payload = {
        "author": f"urn:li:person:{credential['subject']}",
        "commentary": draft.body,
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
        response = httpx.post(
            "https://api.linkedin.com/rest/posts",
            headers={
                "Authorization": f"Bearer {credential['access_token']}",
                "LinkedIn-Version": version,
                "X-Restli-Protocol-Version": "2.0.0",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
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
    except (httpx.HTTPError, LinkedInPublishError) as exc:
        database.mark_publish_failed(draft.id, str(exc))
        if isinstance(exc, LinkedInPublishError):
            raise
        raise LinkedInPublishError(f"LinkedIn request failed: {type(exc).__name__}") from exc
