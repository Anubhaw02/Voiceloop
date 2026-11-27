from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .cards import card_from_json, render_image
from .auth import LinkedInAuthError, LinkedInOAuth, new_csrf_token, new_oauth_state
from .config import Settings, load_settings
from .db import Database
from .linkedin import LinkedInPublishError, publish_linkedin_draft
from .x_client import XError, XOAuth, pkce_pair, publish_x_draft, x_include_link, x_text_problem
from .linkedin_format import BOLD, unicode_bold
from .pipeline import ResearchPipeline
from .workspace import ACTIONABLE, draft_angle, next_scheduled_slot, pillar_label, platform_label, week_start
from .publishing import (
    ApprovalRequiredError,
    PublishingDisabledError,
    export_approved_draft,
)


PACKAGE_ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    database = Database(settings.database_url)
    database.initialize()
    pipeline = ResearchPipeline(settings, database)
    # A full research run takes minutes (live source fetches plus re-scoring),
    # so it runs off the request thread. The lock prevents overlapping runs.
    research_lock = threading.Lock()

    def run_research_in_background() -> None:
        try:
            pipeline.run()
        except Exception:
            LOGGER.exception("Background research run failed")
        finally:
            research_lock.release()

    app = FastAPI(title=settings.raw["app"]["name"], docs_url=None if settings.is_production else "/api/docs")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="frontier_session",
        same_site="lax",
        https_only=settings.is_production,
        max_age=60 * 60 * 12,
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")

    timezone_name = settings.raw["app"].get("timezone", "Asia/Kolkata")
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        local_zone = timezone.utc

    def parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def local_time(value: str | None) -> str:
        parsed = parse_timestamp(value)
        if not parsed:
            return "Time unavailable"
        return parsed.astimezone(local_zone).strftime("%d %b, %I:%M %p %Z")

    def local_date(value: str | None) -> str:
        parsed = parse_timestamp(value)
        if not parsed:
            return "date unavailable"
        return parsed.astimezone(local_zone).strftime("%d %b %Y")

    templates.env.filters["local_time"] = local_time
    templates.env.filters["local_date"] = local_date
    templates.env.filters["platform_label"] = platform_label
    templates.env.filters["pillar_label"] = pillar_label

    def display_run(row: dict[str, object]) -> dict[str, object]:
        prepared = dict(row)
        try:
            errors = json.loads(str(prepared.get("errors_json") or "[]"))
        except (TypeError, ValueError):
            errors = ["The run recorded an unreadable warning."]
        if not isinstance(errors, list):
            errors = [str(errors)]
        prepared["errors"] = [str(error) for error in errors]
        prepared["error_count"] = len(errors)
        prepared["local_started"] = local_time(str(prepared.get("started_at") or ""))
        start = parse_timestamp(prepared.get("started_at"))
        end = parse_timestamp(prepared.get("finished_at"))
        if start and end:
            minutes, seconds = divmod(max(0, int((end - start).total_seconds())), 60)
            prepared["duration"] = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
        try:
            result = json.loads(prepared.get("summary_json") or "null")
            prepared["result"] = result if isinstance(result, dict) else None
        except (TypeError, ValueError):
            prepared["result"] = None
        prepared["label"] = {
            "running": "Running", "failed": "Failed",
            "completed_with_warnings": "Completed with warnings", "completed": "Completed",
        }.get(prepared.get("status"), "Unknown")
        return prepared

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    def require_user(request: Request):
        if settings.auth_enabled and not request.session.get("user"):
            return RedirectResponse(url="/auth/linkedin/login", status_code=303)
        return None

    def csrf_for(request: Request) -> str:
        token = request.session.get("csrf_token")
        if not token:
            token = new_csrf_token()
            request.session["csrf_token"] = token
        return token

    def render_workspace(request: Request, name: str, section: str, **context):
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={
                "section": section, "user": request.session.get("user"),
                "csrf_token": csrf_for(request), **context,
            },
        )

    def pager(page: int, has_next: bool, **filters: str):
        def link(number: int):
            return "?" + urlencode({**{key: value for key, value in filters.items() if value}, "page": number})

        return {
            "number": page,
            "previous": link(page - 1) if page > 1 else None,
            "next": link(page + 1) if has_next else None,
        }

    def require_csrf(request: Request, submitted: str) -> None:
        if not settings.auth_enabled:
            return
        expected = str(request.session.get("csrf_token", ""))
        if not expected or not submitted or not secrets.compare_digest(expected, submitted):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    def queue_status() -> dict[str, dict[str, int | bool]]:
        drafting = settings.raw["drafting"]
        limit = int(drafting.get("max_pending_drafts", 10))
        result: dict[str, dict[str, int | bool]] = {}
        for platform in drafting.get("platforms", ["linkedin"]):
            pending = database.pending_draft_count(platform)
            result[platform] = {
                "pending": pending,
                "limit": limit,
                "full": pending >= limit,
            }
        return result

    def linkedin_connected() -> bool:
        if not settings.linkedin_configured:
            return False
        try:
            return LinkedInOAuth(settings, database).is_connected()
        except LinkedInAuthError:
            return False

    @app.get("/auth/linkedin/login")
    def linkedin_login(request: Request):
        if not settings.linkedin_configured:
            raise HTTPException(status_code=503, detail="LinkedIn OAuth is not configured")
        state, nonce = new_oauth_state()
        request.session["oauth_state"] = state
        request.session["oauth_nonce"] = nonce
        return RedirectResponse(
            LinkedInOAuth(settings, database).authorization_url(state=state, nonce=nonce),
            status_code=303,
        )

    @app.get("/auth/linkedin/callback")
    def linkedin_callback(
        request: Request,
        code: str = "",
        state: str = "",
        error: str = "",
    ):
        if error:
            raise HTTPException(status_code=400, detail=f"LinkedIn denied authorization: {error}")
        expected_state = str(request.session.pop("oauth_state", ""))
        request.session.pop("oauth_nonce", None)
        if not expected_state or not state or not secrets.compare_digest(expected_state, state):
            raise HTTPException(status_code=400, detail="Invalid OAuth state")
        if not code:
            raise HTTPException(status_code=400, detail="LinkedIn did not return an authorization code")
        try:
            user = LinkedInOAuth(settings, database).complete(code=code)
        except LinkedInAuthError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        request.session.clear()
        request.session["user"] = user
        request.session["csrf_token"] = new_csrf_token()
        return RedirectResponse(url="/", status_code=303)

    def x_status() -> dict[str, object]:
        try:
            oauth = XOAuth(settings, database)
        except XError:
            return {"configured": False, "username": None}
        connection = oauth.connection() if oauth.configured else None
        return {"configured": oauth.configured, "username": connection["username"] if connection else None}

    @app.get("/auth/x/login")
    def x_login(request: Request):
        redirect = require_user(request)  # connecting X requires the owner's dashboard session
        if redirect:
            return redirect
        try:
            oauth = XOAuth(settings, database)
        except XError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not oauth.configured:
            raise HTTPException(status_code=503, detail="X is not configured (X_CLIENT_ID / X_CLIENT_SECRET)")
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        request.session["x_oauth_state"] = state
        request.session["x_code_verifier"] = verifier
        return RedirectResponse(oauth.authorization_url(state=state, code_challenge=challenge), status_code=303)

    @app.get("/auth/x/callback")
    def x_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        redirect = require_user(request)
        if redirect:
            return redirect
        expected_state = str(request.session.pop("x_oauth_state", ""))
        verifier = str(request.session.pop("x_code_verifier", ""))
        if error:
            raise HTTPException(status_code=400, detail=f"X denied authorization: {error}")
        if not expected_state or not state or not secrets.compare_digest(expected_state, state) or not verifier:
            raise HTTPException(status_code=400, detail="Invalid X OAuth state; start again from the dashboard")
        if not code:
            raise HTTPException(status_code=400, detail="X did not return an authorization code")
        try:
            XOAuth(settings, database).complete(code=code, code_verifier=verifier)
        except XError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return RedirectResponse(url="/?x=connected", status_code=303)

    @app.post("/auth/x/disconnect")
    def x_disconnect(request: Request, csrf_token: str = Form("")):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        database.delete_oauth_credential("x")
        database.log_event("x_disconnected", "oauth", "x", {})
        return RedirectResponse(url="/", status_code=303)

    @app.post("/auth/logout")
    def logout(request: Request, csrf_token: str = Form("")):
        require_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse(url="/auth/linkedin/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, research: str = "", x: str = ""):
        redirect = require_user(request)
        if redirect:
            return redirect
        queue = queue_status()
        actionable_counts = database.draft_counts_by_platform(ACTIONABLE)
        drafts_by_platform = {
            platform: database.list_drafts(statuses=ACTIONABLE, platform=platform, limit=2)
            for platform in queue
        }
        runs = [display_run(run) for run in database.recent_runs(limit=8)]
        now_local = datetime.now(local_zone)
        schedule = settings.raw["scheduler"].get("cloud_daily_times", [])
        next_run = next_scheduled_slot(now_local, schedule) if settings.is_production else None
        return render_workspace(request, "index.html", "home", **{
                "counts": database.dashboard_counts(),
                "items": database.list_items(limit=3),
                "drafts_by_platform": drafts_by_platform,
                "actionable_counts": actionable_counts,
                "actionable_count": sum(actionable_counts.values()),
                "draft_sources": database.draft_sources([draft.id for group in drafts_by_platform.values() for draft in group]),
                "recent_published": database.list_drafts(status="published", limit=3),
                "published_this_week": database.published_draft_count_since(
                    week_start(now_local).astimezone(timezone.utc).isoformat()
                ),
                "missions": database.list_missions(status="active", limit=1) or database.list_missions(status="proposed", limit=1),
                "runs": runs,
                "latest_run": runs[0] if runs else None,
                "next_run": local_time(next_run.isoformat()) if next_run else None,
                "publish_enabled": settings.publish_enabled and settings.allow_external_writes,
                "linkedin_connected": linkedin_connected(),
                "research_running": research_lock.locked(),
                "research_notice": research if research in {"started", "already_running"} else "",
                "queue": queue,
                "queue_full": bool(queue) and all(status["full"] for status in queue.values()),
                "x": x_status(),
                "x_just_connected": x == "connected",
                "north_star": settings.profile.get("identity", {}).get("identity_statement"),
        })

    @app.get("/drafts", response_class=HTMLResponse)
    def drafts_inbox(request: Request, platform: str = "", view: str = "actionable", page: int = Query(1, ge=1, le=10_000)):
        if redirect := require_user(request):
            return redirect
        platforms = settings.raw["drafting"].get("platforms", ["linkedin"])
        platform = platform if platform in platforms else ""
        views = {
            "actionable": "Needs attention", "pending_approval": "Pending approval",
            "approved": "Approved", "publish_failed": "Publish failed",
            "published": "Published history", "rejected": "Rejected",
        }
        view = view if view in views else "actionable"
        size = 12
        rows = database.list_drafts(
            statuses=ACTIONABLE if view == "actionable" else None,
            status=view if view != "actionable" else None,
            platform=platform, limit=size + 1, offset=(page - 1) * size,
        )
        return render_workspace(
            request, "inbox.html", "drafts", drafts=rows[:size], platforms=platforms,
            platform=platform, view=view, views=views, draft_sources=database.draft_sources([row.id for row in rows[:size]]),
            pager=pager(page, len(rows) > size, platform=platform, view=view),
        )

    @app.get("/research", response_class=HTMLResponse)
    def research_library(
        request: Request, q: str = Query("", max_length=160), topic: str = "", source: str = "",
        sort: str = "relevance", page: int = Query(1, ge=1, le=10_000),
    ):
        if redirect := require_user(request):
            return redirect
        topics, sources = database.research_filters()
        topic = topic if topic in topics else ""
        source = source if source in sources else ""
        sort = "newest" if sort == "newest" else "relevance"
        q = q.strip()
        size = 15
        rows = database.list_items(
            query=q, topic=topic, source_type=source, newest=sort == "newest",
            offset=(page - 1) * size, limit=size + 1,
        )
        return render_workspace(
            request, "research.html", "research", items=rows[:size], q=q,
            topic=topic, source=source, sort=sort, topics=topics, sources=sources,
            pager=pager(page, len(rows) > size, q=q, topic=topic, source=source, sort=sort),
        )

    @app.get("/missions", response_class=HTMLResponse)
    def missions_workspace(request: Request, status: str = "", page: int = Query(1, ge=1, le=10_000)):
        if redirect := require_user(request):
            return redirect
        statuses = {"active": "In progress", "proposed": "Proposed", "completed": "Completed"}
        status = status if status in statuses else ""
        size = 10
        rows = database.list_missions(status=status or None, limit=size + 1, offset=(page - 1) * size)
        return render_workspace(
            request, "missions.html", "missions", missions=rows[:size], status=status, statuses=statuses,
            pager=pager(page, len(rows) > size, status=status),
        )

    @app.get("/brand", response_class=HTMLResponse)
    def brand_workspace(request: Request):
        if redirect := require_user(request):
            return redirect
        now = datetime.now(local_zone)
        monday = week_start(now)
        weekly = database.published_breakdown_since(monday.astimezone(timezone.utc).isoformat())
        recent = database.published_breakdown_since((now - timedelta(days=28)).astimezone(timezone.utc).isoformat())
        content = settings.profile.get("content", {})
        targets = content.get("weekly_post_targets", {})
        topics = {topic: target for topic, target in content.get("pillars", {}).items()}
        for _, topic, _ in recent:
            topics.setdefault(topic, 0)
        total = sum(count for _, _, count in recent)
        return render_workspace(
            request, "brand.html", "brand", identity=settings.profile.get("identity", {}),
            voice=settings.profile.get("voice", {}), audience=settings.profile.get("audience", {}),
            cadence=[
                (platform, targets.get(platform), sum(count for key, _, count in weekly if key == platform))
                for platform in settings.raw["drafting"].get("platforms", ["linkedin"])
            ],
            pillars=[
                (topic, target, count, round(100 * count / total) if total else None)
                for topic, target in topics.items()
                for count in [sum(number for _, key, number in recent if key == topic)]
            ],
            total=total, monday=monday.strftime("%d %b"),
        )

    @app.post("/pipeline/run")
    def run_pipeline(request: Request, csrf_token: str = Form("")):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        if not research_lock.acquire(blocking=False):
            return RedirectResponse(url="/?research=already_running", status_code=303)
        try:
            threading.Thread(target=run_research_in_background, name="research-run", daemon=True).start()
        except Exception:
            research_lock.release()
            raise
        return RedirectResponse(url="/?research=started", status_code=303)

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    def draft_detail(request: Request, draft_id: str):
        redirect = require_user(request)
        if redirect:
            return redirect
        draft = database.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        brief = database.get_brief(draft.brief_id)
        if not brief:
            raise HTTPException(status_code=404, detail="Research brief not found")
        item = database.get_item(brief.item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Research item not found")
        audience, personal_prompt = draft_angle(item.topic)
        return templates.TemplateResponse(
            request=request,
            name="draft.html",
            context={
                "draft": draft,
                "brief": brief,
                "item": item,
                "audience_hint": audience,
                "personal_prompt": personal_prompt,
                "brand_goal": settings.profile.get("identity", {}).get("archetype"),
                "feedback": database.post_feedback(draft_id) if draft.status == "published" else None,
                "feedback_saved": request.query_params.get("feedback") == "saved",
                "claims": database.list_claims(item.id),
                "linkedin_preview": BOLD.sub(lambda match: unicode_bold(match.group(1)), draft.body),
                "card": json.loads(draft.card_json) if draft.card_json else None,
                "publish_enabled": settings.publish_enabled and settings.allow_external_writes,
                "linkedin_connected": linkedin_connected(),
                "x": x_status(),
                "x_problem": x_text_problem(draft.body, include_link=x_include_link(settings)) if draft.platform == "x" else None,
                "csrf_token": csrf_for(request),
            },
        )

    @app.get("/drafts/{draft_id}/card.png")
    def draft_card_image(request: Request, draft_id: str):
        redirect = require_user(request)
        if redirect:
            return redirect
        draft = database.get_draft(draft_id)
        card = card_from_json(draft.card_json) if draft else None
        if not card:
            raise HTTPException(status_code=404, detail="No image for this draft")
        return Response(content=render_image(card), media_type="image/png", headers={"Cache-Control": "private, max-age=60"})

    @app.post("/drafts/{draft_id}/feedback")
    def save_post_feedback(
        request: Request, draft_id: str, impressions: int | None = Form(None, ge=0, le=2_000_000_000),
        comments: int | None = Form(None, ge=0, le=2_000_000_000),
        conversations: int | None = Form(None, ge=0, le=2_000_000_000),
        note: str = Form("", max_length=1200), csrf_token: str = Form(""),
    ):
        if redirect := require_user(request):
            return redirect
        require_csrf(request, csrf_token)
        draft = database.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        if draft.status != "published":
            raise HTTPException(status_code=409, detail="Feedback is only available for published posts")
        if all(value is None for value in (impressions, comments, conversations)) and not note.strip():
            raise HTTPException(status_code=400, detail="Add at least one observation")
        database.log_event("post_feedback", "draft", draft_id, {
            "impressions": impressions, "comments": comments, "conversations": conversations, "note": note.strip(),
        })
        return RedirectResponse(f"/drafts/{draft_id}?feedback=saved#feedback", status_code=303)

    @app.post("/drafts/{draft_id}/card")
    def toggle_draft_card(request: Request, draft_id: str, enabled: str = Form("false"), csrf_token: str = Form("")):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        draft = database.get_draft(draft_id)
        if not draft or not draft.card_json:
            raise HTTPException(status_code=404, detail="No image for this draft")
        card = json.loads(draft.card_json)
        card["enabled"] = enabled == "true"
        if not database.set_draft_card_json(draft_id, json.dumps(card, ensure_ascii=False)):
            raise HTTPException(status_code=409, detail="The image can no longer be changed for this draft")
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/save")
    def save_draft(
        request: Request,
        draft_id: str,
        body: str = Form(...),
        csrf_token: str = Form(""),
    ):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        if not database.get_draft(draft_id):
            raise HTTPException(status_code=404, detail="Draft not found")
        if not database.update_draft_body(draft_id, body.strip()):
            raise HTTPException(status_code=409, detail="Only pending drafts can be edited")
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/approve")
    def approve_draft(
        request: Request,
        draft_id: str,
        body: str = Form(...),
        understood: bool = Form(False),
        checked_claims: bool = Form(False),
        represents_me: bool = Form(False),
        csrf_token: str = Form(""),
    ):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        if not database.get_draft(draft_id):
            raise HTTPException(status_code=404, detail="Draft not found")
        if not (understood and checked_claims and represents_me):
            raise HTTPException(
                status_code=400,
                detail="Understanding, claim review, and voice confirmation are all required.",
            )
        if not database.approve_draft_exact(draft_id, body.strip()):
            return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)
        database.log_event(
            "approval_checklist_completed",
            "draft",
            draft_id,
            {"understood": True, "checked_claims": True, "represents_me": True},
        )
        draft = database.get_draft(draft_id)
        # A disconnected platform leaves the exact approved copy ready to retry.
        if settings.publish_enabled and settings.allow_external_writes and draft and (
            linkedin_connected() if draft.platform == "linkedin" else x_status()["username"]
        ):
            publish_now(draft)
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    def publish_now(draft) -> None:
        """Publishes to the draft's platform; failures are recorded on the draft."""
        try:
            if draft.platform == "x":
                publish_x_draft(database=database, settings=settings, draft=draft)
            else:
                publish_linkedin_draft(database=database, settings=settings, draft=draft)
        except (LinkedInPublishError, XError, ApprovalRequiredError, PublishingDisabledError) as exc:
            current = database.get_draft(draft.id)
            if current and current.status == "publishing":
                database.mark_publish_failed(draft.id, str(exc))

    @app.post("/drafts/{draft_id}/publish")
    def retry_publish(request: Request, draft_id: str, csrf_token: str = Form("")):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        draft = database.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        publish_now(draft)
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/reject")
    def reject_draft(
        request: Request,
        draft_id: str,
        reason: str = Form("Needs revision"),
        csrf_token: str = Form(""),
    ):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        if not database.get_draft(draft_id):
            raise HTTPException(status_code=404, detail="Draft not found")
        if not database.reject_pending_draft(draft_id, reason.strip()):
            raise HTTPException(status_code=409, detail="Only pending drafts can be rejected")
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/export")
    def export_draft(request: Request, draft_id: str, csrf_token: str = Form("")):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        draft = database.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        try:
            export_approved_draft(database=database, settings=settings, draft=draft)
        except ApprovalRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/missions/{mission_id}/activate")
    def activate_mission(request: Request, mission_id: str, csrf_token: str = Form("")):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        database.set_mission_status(mission_id, "active")
        return RedirectResponse(url="/missions", status_code=303)

    @app.post("/missions/{mission_id}/complete")
    def complete_mission(
        request: Request,
        mission_id: str,
        reflection: str = Form(...),
        csrf_token: str = Form(""),
    ):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        if len(reflection.strip()) < 40:
            raise HTTPException(status_code=400, detail="Add a meaningful reflection before completion.")
        database.set_mission_status(mission_id, "completed", reflection=reflection)
        return RedirectResponse(url="/missions", status_code=303)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "database_backend": database.backend,
            "publish_enabled": settings.publish_enabled,
            "external_writes": settings.allow_external_writes,
        }

    return app


app = create_app()
