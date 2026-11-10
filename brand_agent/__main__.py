from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict

import httpx
import uvicorn

from .config import load_settings
from .db import Database
from .pipeline import ResearchPipeline
from .publishing import ApprovalRequiredError, export_approved_draft
from .scheduler import run_scheduler


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="brand-agent",
        description="Local-first Cloud x AI research and approval pipeline.",
    )
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Initialize the local database")
    subparsers.add_parser("run", help="Run one complete research cycle")
    subparsers.add_parser("status", help="Show local queue and recent-run status")
    subparsers.add_parser("doctor", help="Check configuration, local model, and read-only network sources")
    subparsers.add_parser("scheduler", help="Run the local recurring scheduler")

    serve = subparsers.add_parser("serve", help="Start the local approval dashboard")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)

    export = subparsers.add_parser("export", help="Export one approved draft locally")
    export.add_argument("draft_id")
    return parser


def command_doctor(settings) -> int:
    checks: list[dict[str, object]] = []
    checks.append({"check": "database_parent", "ok": settings.database_path.parent.exists()})
    checks.append({"check": "publishing_disabled", "ok": not settings.publish_enabled})
    checks.append(
        {
            "check": "external_writes_disabled",
            "ok": not settings.raw["safety"].get("allow_external_writes", False),
        }
    )
    try:
        response = httpx.get("https://api.openalex.org/works", params={"per-page": 1}, timeout=10)
        checks.append({"check": "openalex_read", "ok": response.status_code == 200})
    except Exception as exc:
        checks.append({"check": "openalex_read", "ok": False, "detail": str(exc)})
    try:
        response = httpx.get(
            f"{settings.raw['drafting']['ollama_url'].rstrip('/')}/api/tags", timeout=2
        )
        checks.append({"check": "ollama_optional", "ok": response.status_code == 200})
    except Exception:
        checks.append(
            {"check": "ollama_optional", "ok": False, "detail": "not installed; safe template mode active"}
        )
    print(json.dumps(checks, indent=2))
    required = [check for check in checks if check["check"] != "ollama_optional"]
    return 0 if all(check["ok"] for check in required) else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    settings = load_settings()
    database = Database(settings.database_path)
    database.initialize()

    if args.command == "init":
        print(f"Initialized local database: {settings.database_path}")
        return 0
    if args.command == "run":
        report = ResearchPipeline(settings, database).run()
        print(json.dumps(asdict(report), indent=2))
        return 0 if report.status != "failed" else 1
    if args.command == "status":
        print(json.dumps({"counts": database.dashboard_counts(), "runs": database.recent_runs()}, indent=2))
        return 0
    if args.command == "doctor":
        return command_doctor(settings)
    if args.command == "scheduler":
        run_scheduler(settings)
        return 0
    if args.command == "serve":
        host = args.host or settings.raw["app"].get("dashboard_host", "127.0.0.1")
        port = args.port or int(settings.raw["app"].get("dashboard_port", 8765))
        uvicorn.run("brand_agent.web:app", host=host, port=port, reload=False)
        return 0
    if args.command == "export":
        draft = database.get_draft(args.draft_id)
        if not draft:
            print(f"Draft not found: {args.draft_id}", file=sys.stderr)
            return 2
        try:
            path = export_approved_draft(database=database, settings=settings, draft=draft)
        except ApprovalRequiredError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        print(path)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

