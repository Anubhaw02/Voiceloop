from __future__ import annotations

import json
import logging
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
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
                "x": x_status(),
                "x_just_connected": x == "connected",
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
                "linkedin_preview": BOLD.sub(lambda match: unicode_bold(match.group(1)), draft.body),
                "card": json.loads(draft.card_json) if draft.card_json else None,
                "publish_enabled": settings.publish_enabled,
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
        # X posts go out on approval only once X is connected; otherwise the
        # approved draft waits for "Publish approved text".
        can_post = draft and (draft.platform == "linkedin" or (draft.platform == "x" and x_status()["username"]))
        if can_post and settings.publish_enabled:
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
