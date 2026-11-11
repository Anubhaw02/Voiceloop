from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    case,
    create_engine,
    desc,
    event,
    func,
    inspect,
    select,
    update,
)
from sqlalchemy.engine import Engine

from .models import Claim, ContentDraft, ResearchBrief, ResearchItem, ResearchMission, utc_now


metadata = MetaData()

research_items = Table(
    "research_items",
    metadata,
    Column("id", String, primary_key=True),
    Column("title", Text, nullable=False),
    Column("abstract", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("source_name", Text, nullable=False),
    Column("source_type", String, nullable=False),
    Column("publication_status", String, nullable=False),
    Column("published_at", String),
    Column("authors_json", Text, nullable=False),
    Column("raw_json", Text, nullable=False),
    Column("content_hash", String, nullable=False, unique=True),
    Column("score", Float, nullable=False, default=0),
    Column("score_json", Text, nullable=False, default="{}"),
    Column("topic", String, nullable=False, default="unclassified"),
    Column("industry", String, nullable=False, default="cross-industry"),
    Column("status", String, nullable=False, default="discovered"),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)
Index("idx_research_score", research_items.c.score)
Index("idx_research_published", research_items.c.published_at)

research_briefs = Table(
    "research_briefs",
    metadata,
    Column("id", String, primary_key=True),
    Column("item_id", String, ForeignKey("research_items.id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("headline", Text, nullable=False),
    Column("problem", Text, nullable=False),
    Column("contribution", Text, nullable=False),
    Column("evidence", Text, nullable=False),
    Column("limitations", Text, nullable=False),
    Column("cloud_implications", Text, nullable=False),
    Column("industry_implications", Text, nullable=False),
    Column("founder_questions_json", Text, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("created_at", String, nullable=False),
)

claims = Table(
    "claims",
    metadata,
    Column("id", String, primary_key=True),
    Column("item_id", String, ForeignKey("research_items.id", ondelete="CASCADE"), nullable=False),
    Column("claim_text", Text, nullable=False),
    Column("source_url", Text, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("verification_status", String, nullable=False),
    Column("created_at", String, nullable=False),
)

drafts = Table(
    "drafts",
    metadata,
    Column("id", String, primary_key=True),
    Column("brief_id", String, ForeignKey("research_briefs.id", ondelete="CASCADE"), nullable=False),
    Column("platform", String, nullable=False),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("risk_level", String, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("approved_at", String),
    Column("rejected_reason", Text),
    Column("published_at", String),
    Column("external_post_id", Text),
    Column("publish_error", Text),
    Column("card_json", Text),
    UniqueConstraint("brief_id", "platform", name="uq_draft_brief_platform"),
)
Index("idx_drafts_status", drafts.c.status, drafts.c.created_at)

pipeline_runs = Table(
    "pipeline_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("started_at", String, nullable=False),
    Column("finished_at", String),
    Column("status", String, nullable=False),
    Column("discovered_count", Integer, nullable=False, default=0),
    Column("brief_count", Integer, nullable=False, default=0),
    Column("draft_count", Integer, nullable=False, default=0),
    Column("errors_json", Text, nullable=False, default="[]"),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_type", String, nullable=False),
    Column("entity_type", String, nullable=False),
    Column("entity_id", String, nullable=False),
    Column("detail_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

missions = Table(
    "missions",
    metadata,
    Column("id", String, primary_key=True),
    Column("item_id", String, ForeignKey("research_items.id", ondelete="CASCADE"), nullable=False, unique=True),
    Column("title", Text, nullable=False),
    Column("objective", Text, nullable=False),
    Column("evidence_required", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("reflection", Text, nullable=False, default=""),
    Column("due_at", String),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)
Index("idx_missions_status", missions.c.status, missions.c.due_at)

oauth_credentials = Table(
    "oauth_credentials",
    metadata,
    Column("provider", String, primary_key=True),
    Column("subject", String, nullable=False),
    Column("email", String, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("access_token_encrypted", Text, nullable=False),
    Column("expires_at", String, nullable=False),
    Column("scope", Text, nullable=False),
    Column("updated_at", String, nullable=False),
)


class Database:
    """Database boundary supporting local SQLite and hosted PostgreSQL."""

    def __init__(self, path_or_url: Path | str):
        value = str(path_or_url)
        if "://" not in value:
            path = Path(value)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.path: Path | None = path
            self.url = f"sqlite+pysqlite:///{path.as_posix()}"
        else:
            self.path = None
            self.url = value
            if self.url.startswith("postgres://"):
                self.url = "postgresql+psycopg://" + self.url[len("postgres://"):]
            elif self.url.startswith("postgresql://"):
                self.url = "postgresql+psycopg://" + self.url[len("postgresql://"):]
            if self.url.startswith("sqlite") and "///" in self.url:
                sqlite_path = Path(self.url.split("///", 1)[1])
                sqlite_path.parent.mkdir(parents=True, exist_ok=True)
                self.path = sqlite_path

        connect_args = {"check_same_thread": False, "timeout": 30} if self.url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(
            self.url,
            future=True,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if self.url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def configure_sqlite(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    @property
    def backend(self) -> str:
        return self.engine.dialect.name

    def initialize(self) -> None:
        metadata.create_all(self.engine)
        # create_all does not add new columns to an existing laptop-era DB.
        existing = {column["name"] for column in inspect(self.engine).get_columns("drafts")}
        additions = {
            "published_at": "TEXT",
            "external_post_id": "TEXT",
            "publish_error": "TEXT",
            "card_json": "TEXT",
        }
        with self.engine.begin() as connection:
            if self.backend == "sqlite":
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            for name, sql_type in additions.items():
                if name not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE drafts ADD COLUMN {name} {sql_type}")

    def start_run(self, run_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                pipeline_runs.insert().values(
                    id=run_id,
                    started_at=utc_now(),
                    status="running",
                    discovered_count=0,
                    brief_count=0,
                    draft_count=0,
                    errors_json="[]",
                )
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
        with self.engine.begin() as connection:
            connection.execute(
                update(pipeline_runs)
                .where(pipeline_runs.c.id == run_id)
                .values(
                    finished_at=utc_now(),
                    status=status,
                    discovered_count=discovered_count,
                    brief_count=brief_count,
                    draft_count=draft_count,
                    errors_json=json.dumps(errors),
                )
            )

    def upsert_item(self, item: ResearchItem) -> bool:
        now = utc_now()
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(research_items.c.id).where(research_items.c.content_hash == item.content_hash)
            ).first()
            if existing:
                connection.execute(
                    update(research_items)
                    .where(research_items.c.content_hash == item.content_hash)
                    .values(
                        abstract=item.abstract,
                        url=item.url,
                        source_name=item.source_name,
                        source_type=item.source_type,
                        publication_status=item.publication_status,
                        published_at=item.published_at,
                        authors_json=json.dumps(item.authors),
                        raw_json=json.dumps(item.raw),
                        updated_at=now,
                    )
                )
                return False
            connection.execute(
                research_items.insert().values(
                    id=item.id,
                    title=item.title,
                    abstract=item.abstract,
                    url=item.url,
                    source_name=item.source_name,
                    source_type=item.source_type,
                    publication_status=item.publication_status,
                    published_at=item.published_at,
                    authors_json=json.dumps(item.authors),
                    raw_json=json.dumps(item.raw),
                    content_hash=item.content_hash,
                    score=item.score,
                    score_json=json.dumps(item.score_breakdown),
                    topic=item.topic,
                    industry=item.industry,
                    status=item.status,
                    created_at=now,
                    updated_at=now,
                )
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
        with self.engine.begin() as connection:
            connection.execute(
                update(research_items)
                .where(research_items.c.id == item_id)
                .values(
                    score=score,
                    score_json=json.dumps(breakdown),
                    topic=topic,
                    industry=industry,
                    status=status,
                    updated_at=utc_now(),
                )
            )

    @staticmethod
    def _item_from_row(row: Mapping[str, Any]) -> ResearchItem:
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
        with self.engine.connect() as connection:
            row = connection.execute(
                select(research_items).where(research_items.c.id == item_id)
            ).mappings().first()
        return self._item_from_row(row) if row else None

    def list_items(
        self,
        *,
        limit: int = 50,
        minimum_score: float = 0,
        include_quarantined: bool = False,
    ) -> list[ResearchItem]:
        conditions = [research_items.c.score >= minimum_score]
        if not include_quarantined:
            conditions.append(research_items.c.status != "quarantined")
        statement = (
            select(research_items)
            .where(and_(*conditions))
            .order_by(desc(research_items.c.score), desc(research_items.c.published_at))
            .limit(limit)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._item_from_row(row) for row in rows]

    def save_brief(self, brief: ResearchBrief) -> None:
        values = {
            "headline": brief.headline,
            "problem": brief.problem,
            "contribution": brief.contribution,
            "evidence": brief.evidence,
            "limitations": brief.limitations,
            "cloud_implications": brief.cloud_implications,
            "industry_implications": brief.industry_implications,
            "founder_questions_json": json.dumps(brief.founder_questions),
            "confidence": brief.confidence,
        }
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(research_briefs.c.id).where(research_briefs.c.item_id == brief.item_id)
            ).first()
            if existing:
                connection.execute(
                    update(research_briefs)
                    .where(research_briefs.c.item_id == brief.item_id)
                    .values(**values)
                )
            else:
                connection.execute(
                    research_briefs.insert().values(
                        id=brief.id,
                        item_id=brief.item_id,
                        created_at=brief.created_at,
                        **values,
                    )
                )

    @staticmethod
    def _brief_from_row(row: Mapping[str, Any]) -> ResearchBrief:
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
        with self.engine.connect() as connection:
            row = connection.execute(
                select(research_briefs).where(research_briefs.c.id == brief_id)
            ).mappings().first()
        return self._brief_from_row(row) if row else None

    def get_brief_for_item(self, item_id: str) -> ResearchBrief | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(research_briefs).where(research_briefs.c.item_id == item_id)
            ).mappings().first()
        return self._brief_from_row(row) if row else None

    def list_briefs(self, *, limit: int = 50) -> list[ResearchBrief]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(research_briefs).order_by(desc(research_briefs.c.created_at)).limit(limit)
            ).mappings().all()
        return [self._brief_from_row(row) for row in rows]

    def save_claim(self, claim: Claim) -> None:
        values = {
            "item_id": claim.item_id,
            "claim_text": claim.claim_text,
            "source_url": claim.source_url,
            "confidence": claim.confidence,
            "verification_status": claim.verification_status,
            "created_at": claim.created_at,
        }
        with self.engine.begin() as connection:
            existing = connection.execute(select(claims.c.id).where(claims.c.id == claim.id)).first()
            if existing:
                connection.execute(update(claims).where(claims.c.id == claim.id).values(**values))
            else:
                connection.execute(claims.insert().values(id=claim.id, **values))

    def list_claims(self, item_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(claims)
                .where(claims.c.item_id == item_id)
                .order_by(desc(claims.c.confidence))
            ).mappings().all()
        return [dict(row) for row in rows]

    def save_draft(self, draft: ContentDraft) -> None:
        values = {
            "title": draft.title,
            "body": draft.body,
            "rationale": draft.rationale,
            "risk_level": draft.risk_level,
            "updated_at": draft.updated_at,
            "card_json": draft.card_json,
        }
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(drafts.c.id, drafts.c.status).where(
                    and_(drafts.c.brief_id == draft.brief_id, drafts.c.platform == draft.platform)
                )
            ).mappings().first()
            if existing:
                if existing["status"] not in {"approved", "exported", "publishing", "published"}:
                    connection.execute(
                        update(drafts)
                        .where(drafts.c.id == existing["id"])
                        .values(status="pending_approval", publish_error=None, **values)
                    )
                return
            connection.execute(
                drafts.insert().values(
                    id=draft.id,
                    brief_id=draft.brief_id,
                    platform=draft.platform,
                    status=draft.status,
                    created_at=draft.created_at,
                    approved_at=draft.approved_at,
                    published_at=draft.published_at,
                    external_post_id=draft.external_post_id,
                    publish_error=draft.publish_error,
                    **values,
                )
            )

    def set_draft_card_json(self, draft_id: str, card_json: str) -> bool:
        """Updates image data unless the draft is already (being) published."""
        with self.engine.begin() as connection:
            result = connection.execute(
                update(drafts)
                .where(and_(drafts.c.id == draft_id, drafts.c.status.notin_(["publishing", "published"])))
                .values(card_json=card_json, updated_at=utc_now())
            )
        return bool(result.rowcount)

    def supersede_pending_drafts(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(drafts)
                .where(drafts.c.status.in_(["pending_approval", "rejected"]))
                .values(status="superseded", updated_at=utc_now())
            )

    @staticmethod
    def _draft_from_row(row: Mapping[str, Any]) -> ContentDraft:
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
            approved_at=row.get("approved_at"),
            published_at=row.get("published_at"),
            external_post_id=row.get("external_post_id"),
            publish_error=row.get("publish_error"),
            card_json=row.get("card_json"),
        )

    def get_draft(self, draft_id: str) -> ContentDraft | None:
        with self.engine.connect() as connection:
            row = connection.execute(select(drafts).where(drafts.c.id == draft_id)).mappings().first()
        return self._draft_from_row(row) if row else None

    def list_drafts(self, *, status: str | None = None, limit: int = 100) -> list[ContentDraft]:
        statement = select(drafts)
        if status:
            statement = statement.where(drafts.c.status == status)
        statement = statement.order_by(desc(drafts.c.created_at)).limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._draft_from_row(row) for row in rows]

    def drafted_item_ids(self) -> set[str]:
        statement = select(research_briefs.c.item_id).select_from(
            drafts.join(research_briefs, research_briefs.c.id == drafts.c.brief_id)
        ).distinct()
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return {row[0] for row in rows}

    def pending_draft_count(self) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.scalar(
                    select(func.count()).select_from(drafts).where(drafts.c.status == "pending_approval")
                )
                or 0
            )

    def update_draft_body(self, draft_id: str, body: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(drafts)
                .where(
                    and_(
                        drafts.c.id == draft_id,
                        drafts.c.status.in_(["pending_approval", "rejected"]),
                    )
                )
                .values(body=body, updated_at=utc_now())
            )
        if result.rowcount:
            self.log_event("draft_edited", "draft", draft_id, {})
        return bool(result.rowcount)

    def approve_draft_exact(self, draft_id: str, body: str) -> bool:
        now = utc_now()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(drafts)
                .where(
                    and_(
                        drafts.c.id == draft_id,
                        drafts.c.status.in_(["pending_approval", "rejected"]),
                    )
                )
                .values(
                    body=body,
                    status="approved",
                    approved_at=now,
                    rejected_reason=None,
                    publish_error=None,
                    updated_at=now,
                )
            )
        if result.rowcount:
            self.log_event("draft_approved", "draft", draft_id, {})
        return bool(result.rowcount)

    def reject_pending_draft(self, draft_id: str, reason: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(drafts)
                .where(
                    and_(
                        drafts.c.id == draft_id,
                        drafts.c.status.in_(["pending_approval", "rejected"]),
                    )
                )
                .values(
                    status="rejected",
                    rejected_reason=reason,
                    updated_at=utc_now(),
                )
            )
        if result.rowcount:
            self.log_event("draft_rejected", "draft", draft_id, {"reason": reason})
        return bool(result.rowcount)

    def set_draft_status(self, draft_id: str, status: str, reason: str | None = None) -> None:
        values: dict[str, Any] = {"status": status, "updated_at": utc_now()}
        if status == "approved":
            values["approved_at"] = utc_now()
        if status == "rejected":
            values["rejected_reason"] = reason
        with self.engine.begin() as connection:
            connection.execute(update(drafts).where(drafts.c.id == draft_id).values(**values))
        self.log_event(f"draft_{status}", "draft", draft_id, {"reason": reason} if reason else {})

    def begin_publishing(self, draft_id: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(drafts)
                .where(
                    and_(
                        drafts.c.id == draft_id,
                        drafts.c.status.in_(["approved", "publish_failed"]),
                    )
                )
                .values(status="publishing", updated_at=utc_now(), publish_error=None)
            )
        return bool(result.rowcount)

    def mark_publish_succeeded(self, draft_id: str, external_post_id: str) -> None:
        now = utc_now()
        with self.engine.begin() as connection:
            connection.execute(
                update(drafts)
                .where(and_(drafts.c.id == draft_id, drafts.c.status == "publishing"))
                .values(
                    status="published",
                    published_at=now,
                    external_post_id=external_post_id,
                    publish_error=None,
                    updated_at=now,
                )
            )
        self.log_event("draft_published", "draft", draft_id, {"external_post_id": external_post_id})

    def mark_publish_failed(self, draft_id: str, error: str) -> None:
        safe_error = error[:2000]
        with self.engine.begin() as connection:
            connection.execute(
                update(drafts)
                .where(drafts.c.id == draft_id)
                .values(status="publish_failed", publish_error=safe_error, updated_at=utc_now())
            )
        self.log_event("draft_publish_failed", "draft", draft_id, {"error": safe_error})

    def save_mission(self, mission: ResearchMission) -> None:
        values = {
            "title": mission.title,
            "objective": mission.objective,
            "evidence_required": mission.evidence_required,
            "updated_at": mission.updated_at,
        }
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(missions.c.id, missions.c.due_at).where(missions.c.item_id == mission.item_id)
            ).mappings().first()
            if existing:
                connection.execute(
                    update(missions)
                    .where(missions.c.item_id == mission.item_id)
                    .values(due_at=existing["due_at"] or mission.due_at, **values)
                )
            else:
                connection.execute(
                    missions.insert().values(
                        id=mission.id,
                        item_id=mission.item_id,
                        status=mission.status,
                        reflection=mission.reflection,
                        due_at=mission.due_at,
                        created_at=mission.created_at,
                        **values,
                    )
                )

    @staticmethod
    def _mission_from_row(row: Mapping[str, Any]) -> ResearchMission:
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
        statement = select(missions)
        if status:
            statement = statement.where(missions.c.status == status)
        priority = case((missions.c.status == "active", 0), (missions.c.status == "proposed", 1), else_=2)
        statement = statement.order_by(priority, missions.c.due_at, desc(missions.c.created_at)).limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [self._mission_from_row(row) for row in rows]

    def set_mission_status(self, mission_id: str, status: str, reflection: str = "") -> None:
        values: dict[str, Any] = {"status": status, "updated_at": utc_now()}
        if reflection:
            values["reflection"] = reflection
        with self.engine.begin() as connection:
            connection.execute(update(missions).where(missions.c.id == mission_id).values(**values))
        self.log_event(
            f"mission_{status}",
            "mission",
            mission_id,
            {"reflection": reflection} if reflection else {},
        )

    def save_oauth_credential(
        self,
        *,
        provider: str,
        subject: str,
        email: str,
        display_name: str,
        access_token_encrypted: str,
        expires_at: str,
        scope: str,
    ) -> None:
        values = {
            "subject": subject,
            "email": email,
            "display_name": display_name,
            "access_token_encrypted": access_token_encrypted,
            "expires_at": expires_at,
            "scope": scope,
            "updated_at": utc_now(),
        }
        with self.engine.begin() as connection:
            exists = connection.execute(
                select(oauth_credentials.c.provider).where(oauth_credentials.c.provider == provider)
            ).first()
            if exists:
                connection.execute(
                    update(oauth_credentials)
                    .where(oauth_credentials.c.provider == provider)
                    .values(**values)
                )
            else:
                connection.execute(oauth_credentials.insert().values(provider=provider, **values))

    def get_oauth_credential(self, provider: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(oauth_credentials).where(oauth_credentials.c.provider == provider)
            ).mappings().first()
        return dict(row) if row else None

    def delete_oauth_credential(self, provider: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(oauth_credentials.delete().where(oauth_credentials.c.provider == provider))

    def log_event(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any],
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                audit_log.insert().values(
                    event_type=event_type,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    detail_json=json.dumps(detail),
                    created_at=utc_now(),
                )
            )

    def last_event_at(self, event_type: str) -> str | None:
        with self.engine.connect() as connection:
            return connection.scalar(
                select(audit_log.c.created_at)
                .where(audit_log.c.event_type == event_type)
                .order_by(desc(audit_log.c.created_at))
                .limit(1)
            )

    def dashboard_counts(self) -> dict[str, int]:
        def count(connection, table, condition=None) -> int:
            statement = select(func.count()).select_from(table)
            if condition is not None:
                statement = statement.where(condition)
            return int(connection.scalar(statement) or 0)

        with self.engine.connect() as connection:
            return {
                "items": count(connection, research_items),
                "briefs": count(connection, research_briefs),
                "pending": count(connection, drafts, drafts.c.status == "pending_approval"),
                "approved": count(
                    connection,
                    drafts,
                    drafts.c.status.in_(["approved", "exported", "publishing", "published"]),
                ),
                "missions": count(connection, missions, missions.c.status.in_(["proposed", "active"])),
            }

    def recent_runs(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(pipeline_runs).order_by(desc(pipeline_runs.c.started_at)).limit(limit)
            ).mappings().all()
        return [dict(row) for row in rows]
