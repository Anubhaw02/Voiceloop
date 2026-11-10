from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings, load_settings
from .db import Database
from .pipeline import ResearchPipeline
from .publishing import ApprovalRequiredError, export_approved_draft


PACKAGE_ROOT = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    database = Database(settings.database_path)
    database.initialize()
    pipeline = ResearchPipeline(settings, database)

    app = FastAPI(title=settings.raw["app"]["name"], docs_url="/api/docs")
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_ROOT / "templates")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
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
            },
        )

    @app.post("/pipeline/run")
    def run_pipeline():
        pipeline.run()
        return RedirectResponse(url="/", status_code=303)

    @app.get("/drafts/{draft_id}", response_class=HTMLResponse)
    def draft_detail(request: Request, draft_id: str):
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
            },
        )

    @app.post("/drafts/{draft_id}/save")
    def save_draft(draft_id: str, body: str = Form(...)):
        if not database.get_draft(draft_id):
            raise HTTPException(status_code=404, detail="Draft not found")
        database.update_draft_body(draft_id, body.strip())
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/approve")
    def approve_draft(
        draft_id: str,
        body: str = Form(...),
        understood: bool = Form(False),
        checked_claims: bool = Form(False),
        represents_me: bool = Form(False),
    ):
        if not database.get_draft(draft_id):
            raise HTTPException(status_code=404, detail="Draft not found")
        if not (understood and checked_claims and represents_me):
            raise HTTPException(
                status_code=400,
                detail="Understanding, claim review, and voice confirmation are all required.",
            )
        database.update_draft_body(draft_id, body.strip())
        database.set_draft_status(draft_id, "approved")
        database.log_event(
            "approval_checklist_completed",
            "draft",
            draft_id,
            {"understood": True, "checked_claims": True, "represents_me": True},
        )
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/reject")
    def reject_draft(draft_id: str, reason: str = Form("Needs revision")):
        if not database.get_draft(draft_id):
            raise HTTPException(status_code=404, detail="Draft not found")
        database.set_draft_status(draft_id, "rejected", reason=reason.strip())
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/drafts/{draft_id}/export")
    def export_draft(draft_id: str):
        draft = database.get_draft(draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        try:
            export_approved_draft(database=database, settings=settings, draft=draft)
        except ApprovalRequiredError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/drafts/{draft_id}", status_code=303)

    @app.post("/missions/{mission_id}/activate")
    def activate_mission(mission_id: str):
        database.set_mission_status(mission_id, "active")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/missions/{mission_id}/complete")
    def complete_mission(mission_id: str, reflection: str = Form(...)):
        if len(reflection.strip()) < 40:
            raise HTTPException(status_code=400, detail="Add a meaningful reflection before completion.")
        database.set_mission_status(mission_id, "completed", reflection=reflection)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "database": str(settings.database_path),
            "publish_enabled": settings.publish_enabled,
            "external_writes": settings.raw["safety"].get("allow_external_writes", False),
            "counts": database.dashboard_counts(),
        }

    return app


app = create_app()

