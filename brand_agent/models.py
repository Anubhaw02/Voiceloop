from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class ResearchItem:
    id: str
    title: str
    abstract: str
    url: str
    source_name: str
    source_type: str
    publication_status: str = "unknown"
    published_at: str | None = None
    authors: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    topic: str = "unclassified"
    industry: str = "cross-industry"
    status: str = "discovered"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResearchBrief:
    id: str
    item_id: str
    headline: str
    problem: str
    contribution: str
    evidence: str
    limitations: str
    cloud_implications: str
    industry_implications: str
    founder_questions: list[str]
    confidence: float
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class Claim:
    id: str
    item_id: str
    claim_text: str
    source_url: str
    confidence: float
    verification_status: str = "source_reported"
    created_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class ContentDraft:
    id: str
    brief_id: str
    platform: str
    title: str
    body: str
    rationale: str
    risk_level: str = "low"
    status: str = "pending_approval"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    approved_at: str | None = None
    published_at: str | None = None
    external_post_id: str | None = None
    publish_error: str | None = None
    card_json: str | None = None  # insight-card image data; None = no image


@dataclass(slots=True)
class ResearchMission:
    id: str
    item_id: str
    title: str
    objective: str
    evidence_required: str
    status: str = "proposed"
    reflection: str = ""
    due_at: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

