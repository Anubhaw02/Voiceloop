from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .models import Claim, ResearchBrief, ResearchItem, ResearchMission


NAMESPACE = uuid.UUID("d353e05d-ab7f-41ec-997d-63de24effb89")


def _sentences(text: str) -> list[str]:
    text = " ".join((text or "").split())
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if len(part.strip()) > 35]


def _choose(
    sentences: list[str],
    terms: Iterable[str],
    fallback: int = 0,
    *,
    numeric_bonus: bool = False,
) -> str:
    ranked_terms = list(terms)
    scored = []
    for index, sentence in enumerate(sentences):
        lowered = sentence.lower()
        score = sum(
            (len(ranked_terms) - term_index) if term in lowered else 0
            for term_index, term in enumerate(ranked_terms)
        )
        if numeric_bonus and re.search(r"\b\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?x\b", sentence):
            score += 8
        score -= index * 0.02
        scored.append((score, sentence))
    if not scored:
        return "The available metadata does not contain enough detail; the full source needs review."
    winner = max(scored, key=lambda pair: pair[0])
    return winner[1] if winner[0] > 0 else sentences[min(fallback, len(sentences) - 1)]


def source_confidence(item: ResearchItem) -> float:
    if item.source_type in {"conference", "journal", "peer_reviewed"}:
        return 0.90
    if item.source_type in {"official_engineering", "official_research"}:
        return 0.78
    if item.source_type == "preprint":
        return 0.68
    if item.source_type == "curated_peer_reviewed":
        return 0.92
    return 0.62


def extract_claims(item: ResearchItem, limit: int = 5) -> list[Claim]:
    sentences = _sentences(item.abstract)
    candidates: list[tuple[int, str]] = []
    for sentence in sentences:
        lowered = sentence.lower()
        score = 0
        if re.search(r"\b\d+(?:\.\d+)?\s?%|\b\d+(?:\.\d+)?x\b", sentence):
            score += 4
        if any(term in lowered for term in ("find", "show", "result", "evaluate", "improve", "reduce")):
            score += 2
        if any(term in lowered for term in ("may", "could", "suggest", "preliminary")):
            score -= 1
        candidates.append((score, sentence))

    claims: list[Claim] = []
    confidence = source_confidence(item)
    for _, sentence in sorted(candidates, key=lambda pair: pair[0], reverse=True)[:limit]:
        claim_id = f"claim_{uuid.uuid5(NAMESPACE, item.id + sentence).hex[:20]}"
        claims.append(
            Claim(
                id=claim_id,
                item_id=item.id,
                claim_text=sentence,
                source_url=item.url,
                confidence=confidence,
                verification_status="reported_by_source_not_independently_reproduced",
            )
        )
    return claims


def build_brief(item: ResearchItem) -> ResearchBrief:
    sentences = _sentences(item.abstract)
    problem = _choose(
        sentences,
        ("challenge", "bottleneck", "problem", "gap", "lack", "difficult", "failure", "cost"),
    )
    contribution = _choose(
        sentences,
        ("we introduce", "we present", "we propose", "we develop", "introduce", "framework", "system", "benchmark"),
        fallback=1,
    )
    evidence = _choose(
        sentences,
        ("improve", "reduce", "outperform", "result", "achieve", "evaluate", "production", "dataset"),
        fallback=2,
        numeric_bonus=True,
    )

    if item.publication_status.startswith("preprint"):
        limitations = (
            "This is a preprint and may change after peer review. The brief uses source-provided "
            "metadata and abstract claims; methods, baselines, and external reproduction require review."
        )
    elif "official" in item.publication_status:
        limitations = (
            "This is an official organization source rather than independent peer review. Treat reported "
            "outcomes as a production signal and verify comparisons, costs, and generalizability separately."
        )
    else:
        limitations = (
            "The brief is generated from source metadata and the abstract. Verify the full paper's methods, "
            "sample selection, baselines, and limitations before making a strong public claim."
        )

    topic_implications = {
        "ai_for_cloud": (
            "This work may influence incident response, SRE automation, FinOps, observability, or the design "
            "of bounded agents operating on cloud infrastructure."
        ),
        "cloud_for_ai": (
            "This work may affect inference cost, latency, accelerator utilization, reliability, and the "
            "architecture required to operate AI products at scale."
        ),
        "ai_in_industry": (
            "The practical question is whether the technique improves a measurable workflow while satisfying "
            "industry constraints such as privacy, safety, integration cost, and human oversight."
        ),
        "agent_reliability": (
            "The result matters for how production agents are evaluated, constrained, observed, and granted "
            "permission to take consequential actions."
        ),
    }
    cloud_implications = topic_implications.get(
        item.topic,
        "Assess its effect on cloud cost, scalability, reliability, security, and operational complexity.",
    )
    industry_implications = (
        f"The most relevant initial market is {item.industry.replace('_', ' ')}. A useful application needs "
        "a specific buyer, a measurable workflow outcome, and evidence that the benefit survives production constraints."
    )
    questions = [
        "Which production bottleneck does this remove, and for whom?",
        "What must be true for the reported result to generalize beyond the evaluation environment?",
        "Can a small implementation or benchmark test the central claim locally?",
        "Where should human approval remain if this system can take consequential actions?",
    ]
    brief_id = f"brief_{uuid.uuid5(NAMESPACE, item.id).hex[:20]}"
    return ResearchBrief(
        id=brief_id,
        item_id=item.id,
        headline=item.title,
        problem=problem,
        contribution=contribution,
        evidence=evidence,
        limitations=limitations,
        cloud_implications=cloud_implications,
        industry_implications=industry_implications,
        founder_questions=questions,
        confidence=source_confidence(item),
    )


def build_mission(item: ResearchItem, brief: ResearchBrief) -> ResearchMission:
    mission_id = f"mission_{uuid.uuid5(NAMESPACE, 'mission:' + item.id).hex[:20]}"
    objectives = {
        "ai_for_cloud": (
            "Read the complete source and map its incident workflow: signals, diagnosis, proposed action, "
            "permission boundary, rollback path, and human checkpoint."
        ),
        "cloud_for_ai": (
            "Read the complete source and reconstruct the serving architecture, resource bottleneck, "
            "measurement method, and cost/latency/reliability trade-off."
        ),
        "ai_in_industry": (
            "Identify the buyer, existing workflow, measurable outcome, required cloud architecture, and "
            "the strongest reason the application might fail in deployment."
        ),
        "agent_reliability": (
            "Identify the failure model, evaluation method, permission boundaries, and evidence required "
            "before granting the agent more autonomy."
        ),
    }
    evidence = (
        "Write a 150-word explanation in your own words and record one disagreement, unresolved question, "
        "or small experiment that could test the central claim."
    )
    return ResearchMission(
        id=mission_id,
        item_id=item.id,
        title=f"Understand and challenge: {item.title}",
        objective=objectives.get(
            item.topic,
            "Read the complete source and connect its central claim to a real Cloud x AI production decision.",
        ),
        evidence_required=evidence,
        due_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
    )

