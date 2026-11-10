from __future__ import annotations

import math
import re
from html import unescape
from datetime import datetime, timezone
from typing import Any

from .models import ResearchItem


PEER_REVIEWED_TYPES = {"conference", "journal", "peer_reviewed", "curated_peer_reviewed"}
PREPRINT_TYPES = {"preprint", "arxiv"}
OFFICIAL_TYPES = {"official_engineering", "official_research"}


def _text(item: ResearchItem) -> str:
    # Long vendor posts can contain every preferred keyword and drown out concise
    # papers. Score the title plus a bounded evidence window instead.
    return f"{item.title} {item.abstract[:1800]}".lower()


def _term_coverage(text: str, terms: list[str]) -> float:
    if not terms:
        return 0.0
    matches = sum(1 for term in terms if term.lower() in text)
    return min(1.0, matches / max(2, min(6, len(terms))))


def _source_credibility(item: ResearchItem) -> float:
    status = item.publication_status.lower()
    source_type = item.source_type.lower()
    if source_type in PEER_REVIEWED_TYPES or "peer" in status:
        return 1.0
    if source_type in OFFICIAL_TYPES:
        return 0.78
    if source_type in PREPRINT_TYPES or "preprint" in status:
        return 0.68
    return 0.55


def _recency(published_at: str | None) -> float:
    if not published_at:
        return 0.4
    try:
        normalized = published_at.replace("Z", "+00:00")
        published = datetime.fromisoformat(normalized)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_days = max(0, (datetime.now(timezone.utc) - published).days)
    except ValueError:
        return 0.4
    return max(0.1, math.exp(-age_days / 180))


def classify_topic(text: str) -> str:
    if any(term in text for term in ("aiops", "sre", "incident", "root cause", "cloud operations", "finops")):
        return "ai_for_cloud"
    if any(
        term in text
        for term in (
            "model serving",
            "inference serving",
            "gpu scheduling",
            "accelerator cluster",
            "distributed training",
            "serverless llm",
            "kv cache",
        )
    ):
        return "cloud_for_ai"
    categories = {
        "ai_for_cloud": ["aiops", "sre", "incident", "root cause", "cloud operations", "finops"],
        "cloud_for_ai": [
            "model serving",
            "inference",
            "gpu",
            "accelerator",
            "distributed training",
            "serverless",
            "kv cache",
        ],
        "ai_in_industry": [
            "healthcare",
            "finance",
            "manufacturing",
            "energy",
            "enterprise",
            "supply chain",
            "robotics",
        ],
        "agent_reliability": ["agent", "agentic", "reliability", "tool use", "benchmark"],
    }
    scored = {
        category: sum(text.count(term) for term in terms) for category, terms in categories.items()
    }
    winner = max(scored, key=scored.get)
    return winner if scored[winner] > 0 else "cloud_ai_general"


def classify_industry(text: str) -> str:
    industries = {
        "financial_services": ["finance", "bank", "fintech", "insurance", "trading"],
        "healthcare": ["healthcare", "medical", "clinical", "patient", "drug"],
        "manufacturing": ["manufacturing", "factory", "industrial", "predictive maintenance"],
        "energy": ["energy", "power grid", "electricity", "renewable"],
        "developer_platforms": ["developer", "software engineering", "coding", "devops", "sre"],
        "enterprise_software": ["enterprise", "workflow", "office", "business process"],
    }
    scored = {
        industry: sum(text.count(term) for term in terms) for industry, terms in industries.items()
    }
    winner = max(scored, key=scored.get)
    return winner if scored[winner] > 0 else "cross_industry"


def score_item(item: ResearchItem, settings: dict[str, Any]) -> tuple[float, dict[str, float]]:
    text = _text(item)
    terms = settings["preferred_terms"]
    ai = _term_coverage(text, terms["ai"])
    cloud = _term_coverage(text, terms["cloud"])
    production = _term_coverage(text, terms["production"])
    founder = _term_coverage(text, terms["founder"])
    actionable = _term_coverage(text, terms["actionability"])

    intersection = math.sqrt(max(ai, 0.01) * max(cloud, 0.01))
    relevance = min(1.0, 0.65 * intersection + 0.35 * production)
    credibility = _source_credibility(item)
    industry_value = min(1.0, 0.65 * production + 0.35 * founder)
    novelty = min(1.0, 0.70 * _recency(item.published_at) + 0.30 * actionable)
    founder_value = min(1.0, 0.55 * founder + 0.45 * production)
    actionability = actionable

    components = {
        "relevance": relevance,
        "credibility": credibility,
        "industry_value": industry_value,
        "novelty": novelty,
        "founder_value": founder_value,
        "actionability": actionability,
    }
    weights = settings["weights"]
    weighted = sum(components[name] * float(weights[name]) for name in components)

    # Penalize items that only mention one half of the Cloud x AI intersection.
    if ai < 0.15 or cloud < 0.15:
        weighted *= 0.72

    lowered_title = item.title.lower()
    if any(
        phrase in lowered_title
        for phrase in (
            "what's new",
            "what’s new",
            "what�s new",
            "weekly roundup",
            "monthly roundup",
            "year in review",
        )
    ):
        weighted *= 0.55

    breakdown = {name: round(value * 100, 1) for name, value in components.items()}
    breakdown["ai_match"] = round(ai * 100, 1)
    breakdown["cloud_match"] = round(cloud * 100, 1)
    return round(weighted * 100, 2), breakdown


def clean_text(value: str) -> str:
    value = unescape(value or "")
    value = re.sub(r"\b(?:aside_block|content_block)\s+<ListValue:.*?\]>", " ", value, flags=re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("�", "'")
    value = re.sub(r"\s+", " ", value)
    return value.strip()

