from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import Claim, ContentDraft, ResearchBrief, ResearchItem, ResearchMission, utc_now


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS research_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    abstract TEXT NOT NULL,
    url TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_type TEXT NOT NULL,
    publication_status TEXT NOT NULL,
    published_at TEXT,
    authors_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    score REAL NOT NULL DEFAULT 0,
    score_json TEXT NOT NULL DEFAULT '{}',
    topic TEXT NOT NULL DEFAULT 'unclassified',
    industry TEXT NOT NULL DEFAULT 'cross-industry',
    status TEXT NOT NULL DEFAULT 'discovered',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_score ON research_items(score DESC);
CREATE INDEX IF NOT EXISTS idx_research_published ON research_items(published_at DESC);

CREATE TABLE IF NOT EXISTS research_briefs (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE REFERENCES research_items(id) ON DELETE CASCADE,
    headline TEXT NOT NULL,
    problem TEXT NOT NULL,
    contribution TEXT NOT NULL,
    evidence TEXT NOT NULL,
    limitations TEXT NOT NULL,
    cloud_implications TEXT NOT NULL,
    industry_implications TEXT NOT NULL,
    founder_questions_json TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES research_items(id) ON DELETE CASCADE,
    claim_text TEXT NOT NULL,
    source_url TEXT NOT NULL,
    confidence REAL NOT NULL,
    verification_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    brief_id TEXT NOT NULL REFERENCES research_briefs(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    rationale TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    rejected_reason TEXT,
    UNIQUE(brief_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status, created_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    brief_count INTEGER NOT NULL DEFAULT 0,
    draft_count INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE REFERENCES research_items(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    objective TEXT NOT NULL,
    evidence_required TEXT NOT NULL,
    status TEXT NOT NULL,
    reflection TEXT NOT NULL DEFAULT '',
    due_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status, due_at);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def start_run(self, run_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO pipeline_runs(id, started_at, status) VALUES (?, ?, ?)",
                (run_id, utc_now(), "running"),
            )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        discovered_count: int,
        brief_count: int,
        draft_count: int,
        errors: list[str],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE pipeline_runs
                SET finished_at=?, status=?, discovered_count=?, brief_count=?,
                    draft_count=?, errors_json=?
                WHERE id=?
                """,
                (
                    utc_now(),
                    status,
                    discovered_count,
                    brief_count,
                    draft_count,
                    json.dumps(errors),
                    run_id,
                ),
            )

    def upsert_item(self, item: ResearchItem) -> bool:
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM research_items WHERE content_hash=?", (item.content_hash,)
            ).fetchone()
            if existing:
                connection.execute(
                    """
                    UPDATE research_items SET
                        abstract=?, url=?, source_name=?, source_type=?,
                        publication_status=?, published_at=?, authors_json=?, raw_json=?,
                        updated_at=?
                    WHERE content_hash=?
                    """,
                    (
                        item.abstract,
                        item.url,
                        item.source_name,
                        item.source_type,
                        item.publication_status,
                        item.published_at,
                        json.dumps(item.authors),
                        json.dumps(item.raw),
                        now,
                        item.content_hash,
                    ),
                )
                return False

            connection.execute(
                """
                INSERT INTO research_items(
                    id, title, abstract, url, source_name, source_type,
                    publication_status, published_at, authors_json, raw_json,
                    content_hash, score, score_json, topic, industry, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.title,
                    item.abstract,
                    item.url,
                    item.source_name,
                    item.source_type,
                    item.publication_status,
                    item.published_at,
                    json.dumps(item.authors),
                    json.dumps(item.raw),
                    item.content_hash,
                    item.score,
                    json.dumps(item.score_breakdown),
                    item.topic,
                    item.industry,
                    item.status,
                    now,
                    now,
                ),
            )
            return True

    def update_item_analysis(
        self,
        item_id: str,
        *,
        score: float,
        breakdown: dict[str, float],
        topic: str,
        industry: str,
        status: str = "scored",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE research_items
                SET score=?, score_json=?, topic=?, industry=?, status=?, updated_at=?
                WHERE id=?
                """,
                (score, json.dumps(breakdown), topic, industry, status, utc_now(), item_id),
            )

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> ResearchItem:
        return ResearchItem(
            id=row["id"],
            title=row["title"],
            abstract=row["abstract"],
            url=row["url"],
            source_name=row["source_name"],
            source_type=row["source_type"],
            publication_status=row["publication_status"],
            published_at=row["published_at"],
            authors=json.loads(row["authors_json"]),
            raw=json.loads(row["raw_json"]),
            content_hash=row["content_hash"],
            score=row["score"],
            score_breakdown=json.loads(row["score_json"]),
            topic=row["topic"],
            industry=row["industry"],
            status=row["status"],
        )

    def get_item(self, item_id: str) -> ResearchItem | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_items WHERE id=?", (item_id,)
            ).fetchone()
        return self._item_from_row(row) if row else None

    def list_items(
        self,
        *,
        limit: int = 50,
        minimum_score: float = 0,
        include_quarantined: bool = False,
    ) -> list[ResearchItem]:
        status_clause = "" if include_quarantined else "AND status != 'quarantined'"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM research_items
                WHERE score >= ?
                {status_clause}
                ORDER BY score DESC, published_at DESC
                LIMIT ?
                """,
                (minimum_score, limit),
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def save_brief(self, brief: ResearchBrief) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO research_briefs(
                    id, item_id, headline, problem, contribution, evidence,
                    limitations, cloud_implications, industry_implications,
                    founder_questions_json, confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    headline=excluded.headline,
                    problem=excluded.problem,
                    contribution=excluded.contribution,
                    evidence=excluded.evidence,
                    limitations=excluded.limitations,
                    cloud_implications=excluded.cloud_implications,
                    industry_implications=excluded.industry_implications,
                    founder_questions_json=excluded.founder_questions_json,
                    confidence=excluded.confidence
                """,
                (
                    brief.id,
                    brief.item_id,
                    brief.headline,
                    brief.problem,
                    brief.contribution,
                    brief.evidence,
                    brief.limitations,
                    brief.cloud_implications,
                    brief.industry_implications,
                    json.dumps(brief.founder_questions),
                    brief.confidence,
                    brief.created_at,
                ),
            )

    @staticmethod
    def _brief_from_row(row: sqlite3.Row) -> ResearchBrief:
        return ResearchBrief(
            id=row["id"],
            item_id=row["item_id"],
            headline=row["headline"],
            problem=row["problem"],
            contribution=row["contribution"],
            evidence=row["evidence"],
            limitations=row["limitations"],
            cloud_implications=row["cloud_implications"],
            industry_implications=row["industry_implications"],
            founder_questions=json.loads(row["founder_questions_json"]),
            confidence=row["confidence"],
            created_at=row["created_at"],
        )

    def get_brief(self, brief_id: str) -> ResearchBrief | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_briefs WHERE id=?", (brief_id,)
            ).fetchone()
        return self._brief_from_row(row) if row else None

    def get_brief_for_item(self, item_id: str) -> ResearchBrief | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_briefs WHERE item_id=?", (item_id,)
            ).fetchone()
        return self._brief_from_row(row) if row else None

    def list_briefs(self, *, limit: int = 50) -> list[ResearchBrief]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM research_briefs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._brief_from_row(row) for row in rows]

    def save_claim(self, claim: Claim) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO claims(
                    id, item_id, claim_text, source_url, confidence,
                    verification_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim.id,
                    claim.item_id,
                    claim.claim_text,
                    claim.source_url,
                    claim.confidence,
                    claim.verification_status,
                    claim.created_at,
                ),
            )

    def list_claims(self, item_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM claims WHERE item_id=? ORDER BY confidence DESC", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def save_draft(self, draft: ContentDraft) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO drafts(
                    id, brief_id, platform, title, body, rationale, risk_level,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(brief_id, platform) DO UPDATE SET
                    title=excluded.title,
                    body=excluded.body,
                    rationale=excluded.rationale,
                    risk_level=excluded.risk_level,
                    status='pending_approval',
                    updated_at=excluded.updated_at
                WHERE drafts.status NOT IN ('approved', 'exported')
                """,
                (
                    draft.id,
                    draft.brief_id,
                    draft.platform,
                    draft.title,
                    draft.body,
                    draft.rationale,
                    draft.risk_level,
                    draft.status,
                    draft.created_at,
                    draft.updated_at,
                ),
            )

    def supersede_pending_drafts(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE drafts
                SET status='superseded', updated_at=?
                WHERE status IN ('pending_approval', 'rejected')
                """,
                (utc_now(),),
            )

    @staticmethod
    def _draft_from_row(row: sqlite3.Row) -> ContentDraft:
        return ContentDraft(
            id=row["id"],
            brief_id=row["brief_id"],
            platform=row["platform"],
            title=row["title"],
            body=row["body"],
            rationale=row["rationale"],
            risk_level=row["risk_level"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_draft(self, draft_id: str) -> ContentDraft | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return self._draft_from_row(row) if row else None

    def list_drafts(self, *, status: str | None = None, limit: int = 100) -> list[ContentDraft]:
        query = "SELECT * FROM drafts"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._draft_from_row(row) for row in rows]

    def drafted_item_ids(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT b.item_id
                FROM drafts d
                JOIN research_briefs b ON b.id = d.brief_id
                """
            ).fetchall()
        return {row[0] for row in rows}

    def pending_draft_count(self) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM drafts WHERE status='pending_approval'"
                ).fetchone()[0]
            )

    def update_draft_body(self, draft_id: str, body: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE drafts SET body=?, updated_at=? WHERE id=?",
                (body, utc_now(), draft_id),
            )
        self.log_event("draft_edited", "draft", draft_id, {})

    def set_draft_status(self, draft_id: str, status: str, reason: str | None = None) -> None:
        approved_at = utc_now() if status == "approved" else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE drafts
                SET status=?, updated_at=?, approved_at=?, rejected_reason=?
                WHERE id=?
                """,
                (status, utc_now(), approved_at, reason, draft_id),
            )
        self.log_event(
            f"draft_{status}", "draft", draft_id, {"reason": reason} if reason else {}
        )

    def save_mission(self, mission: ResearchMission) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO missions(
                    id, item_id, title, objective, evidence_required, status,
                    reflection, due_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    title=excluded.title,
                    objective=excluded.objective,
                    evidence_required=excluded.evidence_required,
                    due_at=COALESCE(missions.due_at, excluded.due_at),
                    updated_at=excluded.updated_at
                """,
                (
                    mission.id,
                    mission.item_id,
                    mission.title,
                    mission.objective,
                    mission.evidence_required,
                    mission.status,
                    mission.reflection,
                    mission.due_at,
                    mission.created_at,
                    mission.updated_at,
                ),
            )

    @staticmethod
    def _mission_from_row(row: sqlite3.Row) -> ResearchMission:
        return ResearchMission(
            id=row["id"],
            item_id=row["item_id"],
            title=row["title"],
            objective=row["objective"],
            evidence_required=row["evidence_required"],
            status=row["status"],
            reflection=row["reflection"],
            due_at=row["due_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_missions(self, *, status: str | None = None, limit: int = 20) -> list[ResearchMission]:
        query = "SELECT * FROM missions"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'proposed' THEN 1 ELSE 2 END, due_at, created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._mission_from_row(row) for row in rows]

    def set_mission_status(self, mission_id: str, status: str, reflection: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE missions
                SET status=?, reflection=?, updated_at=?
                WHERE id=?
                """,
                (status, reflection.strip(), utc_now(), mission_id),
            )
        self.log_event(
            f"mission_{status}",
            "mission",
            mission_id,
            {"reflection": reflection.strip()} if reflection.strip() else {},
        )

    def log_event(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_log(event_type, entity_type, entity_id, detail_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_type, entity_type, entity_id, json.dumps(detail), utc_now()),
            )

    def dashboard_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            item_count = connection.execute("SELECT COUNT(*) FROM research_items").fetchone()[0]
            brief_count = connection.execute("SELECT COUNT(*) FROM research_briefs").fetchone()[0]
            pending_count = connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE status='pending_approval'"
            ).fetchone()[0]
            approved_count = connection.execute(
                "SELECT COUNT(*) FROM drafts WHERE status IN ('approved', 'exported')"
            ).fetchone()[0]
            mission_count = connection.execute(
                "SELECT COUNT(*) FROM missions WHERE status IN ('proposed', 'active')"
            ).fetchone()[0]
        return {
            "items": item_count,
            "briefs": brief_count,
            "pending": pending_count,
            "approved": approved_count,
            "missions": mission_count,
        }

    def recent_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

