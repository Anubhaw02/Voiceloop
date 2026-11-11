from __future__ import annotations

import html
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .db import Database


class NotificationError(RuntimeError):
    pass


def _token_message(database: Database) -> str:
    credential = database.get_oauth_credential("linkedin")
    if not credential:
        return "LinkedIn is not connected yet. Connect it from the dashboard before publishing."
    try:
        expiry = datetime.fromisoformat(credential["expires_at"])
    except (TypeError, ValueError):
        return "LinkedIn authorization has an invalid expiry and must be reconnected."
    remaining = expiry - datetime.now(timezone.utc)
    if remaining <= timedelta(0):
        return "LinkedIn authorization has expired. Reconnect it from the dashboard."
    if remaining <= timedelta(days=7):
        return f"LinkedIn authorization expires in {max(0, remaining.days)} day(s). Reconnect soon."
    return f"LinkedIn authorization is connected until {expiry.date().isoformat()}."


def send_pending_review_reminder(
    *,
    settings: Settings,
    database: Database,
    force: bool = False,
) -> dict[str, object]:
    pending = database.pending_draft_count()
    if pending == 0:
        return {"status": "not_needed", "pending": 0}

    last_sent = database.last_event_at("review_reminder_sent")
    if last_sent and not force:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(last_sent) < timedelta(hours=20):
                return {"status": "already_sent_recently", "pending": pending}
        except ValueError:
            pass

    api_key = os.getenv("RESEND_API_KEY", "").strip()
    recipient = os.getenv("REMINDER_EMAIL", "").strip()
    sender = os.getenv(
        "RESEND_FROM_EMAIL",
        "Frontier Brand Agent <onboarding@resend.dev>",
    ).strip()
    if not api_key or not recipient:
        return {
            "status": "not_configured",
            "pending": pending,
            "missing": [
                name
                for name, value in (("RESEND_API_KEY", api_key), ("REMINDER_EMAIL", recipient))
                if not value
            ],
        }

    token_message = _token_message(database)
    subject = f"{pending} Frontier Brand draft{'s' if pending != 1 else ''} need your review"
    dashboard_url = settings.base_url
    body = f"""
    <div style="font-family:Arial,sans-serif;max-width:620px;margin:auto;line-height:1.55">
      <h2>Your research cycle is ready</h2>
      <p><strong>{pending}</strong> draft{'s are' if pending != 1 else ' is'} waiting for your judgment.</p>
      <p>{html.escape(token_message)}</p>
      <p><a href="{html.escape(dashboard_url)}" style="display:inline-block;padding:12px 18px;background:#0a66c2;color:#fff;text-decoration:none;border-radius:6px">Review drafts</a></p>
      <p style="color:#555">Nothing is published until you review the research, confirm all three approval checks, and approve the exact text.</p>
    </div>
    """
    local_day = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": f"frontier-review-{local_day}",
            },
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "html": body,
            },
            timeout=20,
        )
        if response.status_code not in {200, 201}:
            raise NotificationError(f"Resend returned HTTP {response.status_code}: {response.text[:500]}")
        response_data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise NotificationError(f"Reminder email failed: {type(exc).__name__}") from exc

    database.log_event(
        "review_reminder_sent",
        "draft_queue",
        "pending",
        {"pending": pending, "recipient": recipient, "resend_id": response_data.get("id")},
    )
    return {"status": "sent", "pending": pending, "id": response_data.get("id")}
