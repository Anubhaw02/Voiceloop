from __future__ import annotations

import logging
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import LinkedInAuthError, LinkedInOAuth, new_csrf_token, new_oauth_state
from .config import Settings, load_settings
from .db import Database
from .linkedin import LinkedInPublishError, publish_linkedin_draft
from .pipeline import ResearchPipeline
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

    def require_csrf(request: Request, submitted: str) -> None:
        if not settings.auth_enabled:
            return
        expected = str(request.session.get("csrf_token", ""))
        if not expected or not submitted or not secrets.compare_digest(expected, submitted):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")

    def queue_full() -> bool:
        # Each selected topic needs one slot per platform (LinkedIn + X), so
        # the queue is effectively full before it reaches the hard limit.
        drafting = settings.raw["drafting"]
        platforms = max(1, len(drafting.get("platforms", ["linkedin"])))
        free = int(drafting.get("max_pending_drafts", 10)) - database.pending_draft_count()
        return free // platforms == 0

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

    @app.post("/auth/logout")
    def logout(request: Request, csrf_token: str = Form("")):
        require_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse(url="/auth/linkedin/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, research: str = ""):
        redirect = require_user(request)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "counts": database.dashboard_counts(),
                "items": database.list_items(limit=12),
                "drafts": database.list_drafts(limit=20),
                "missions": database.list_missions(limit=10),
                "runs": database.recent_runs(limit=8),
                "publish_enabled": settings.publish_enabled,
                "linkedin_connected": linkedin_connected(),
                "user": request.session.get("user"),
                "csrf_token": csrf_for(request),
                "research_running": research_lock.locked(),
                "research_notice": research if research in {"started", "already_running"} else "",
                "queue_full": queue_full(),
                "max_pending_drafts": int(settings.raw["drafting"].get("max_pending_drafts", 10)),
            },
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
        return templates.TemplateResponse(
            request=request,
            name="draft.html",
            context={
                "draft": draft,
                "brief": brief,
                "item": item,
                "claims": database.list_claims(item.id),
                "publish_enabled": settings.publish_enabled,
                "linkedin_connected": linkedin_connected(),
                "csrf_token": csrf_for(request),
            },
        )

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
        if draft and draft.platform == "linkedin" and settings.publish_enabled:
            try:
                publish_linkedin_draft(database=database, settings=settings, draft=draft)
            except (LinkedInPublishError, ApprovalRequiredError, PublishingDisabledError) as exc:
                current = database.get_draft(draft_id)
                if current and current.status == "publishing":
                    database.mark_publish_failed(draft_id, str(exc))
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/publish")
    def retry_publish(request: Request, draft_id: str, csrf_token: str = Form("")):
        redirect = require_user(request)
        if redirect:
            return redirect
        require_csrf(request, csrf_token)
        draft = database.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        try:
            publish_linkedin_draft(database=database, settings=settings, draft=draft)
        except (LinkedInPublishError, ApprovalRequiredError, PublishingDisabledError) as exc:
            current = database.get_draft(draft_id)
            if current and current.status == "publishing":
                database.mark_publish_failed(draft_id, str(exc))
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
        return RedirectResponse(url="/", status_code=303)

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
        return RedirectResponse(url="/", status_code=303)

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
