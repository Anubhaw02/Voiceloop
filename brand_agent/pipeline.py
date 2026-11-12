from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from .ai_research import DraftResult, ai_brief, ai_drafts, judge_items
from .config import Settings
from .db import Database
from .drafting import build_draft, choose_provider, create_draft
from .fulltext import fetch_full_text
from .llm import LLMError, LLMRouter, build_router
from .x_client import x_include_link
from .models import ResearchItem
from .research import build_brief, build_mission, extract_claims
from .scoring import classify_industry, classify_topic, score_item
from .safety import scan_untrusted_text
from .sources import build_sources, discover_all


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineReport:
    run_id: str
    status: str
    discovered: int = 0
    new_items: int = 0
    briefs: int = 0
    drafts: int = 0
    ai_providers: list[str] = field(default_factory=list)
    ai_calls: int = 0
    ai_judged: int = 0
    ai_briefs: int = 0
    ai_drafts: int = 0
    ai_skipped: int = 0
    errors: list[str] = field(default_factory=list)


JUDGE_SHORTLIST = 20


class ResearchPipeline:
    def __init__(self, settings: Settings, database: Database | None = None):
        self.settings = settings
        self.database = database or Database(settings.database_url)
        self.database.initialize()

    def run(self) -> PipelineReport:
        run_id = f"run_{uuid.uuid4().hex[:20]}"
        report = PipelineReport(run_id=run_id, status="running")
        self.database.start_run(run_id)
        try:
            sources = build_sources(self.settings.raw["research"], self.settings.project_root)
            discovery = discover_all(sources)
            report.discovered = len(discovery.items)
            report.errors.extend(discovery.errors)

            for item in discovery.items:
                score, breakdown = score_item(item, self.settings.raw["scoring"])
                safety_findings = scan_untrusted_text(item.title, item.abstract)
                if safety_findings:
                    score = 0
                    breakdown["safety_quarantine"] = 100.0
                    item.raw["safety_findings"] = [finding.rule for finding in safety_findings]
                    item.status = "quarantined"
                item.score = score
                item.score_breakdown = breakdown
                combined_text = f"{item.title} {item.abstract}".lower()
                item.topic = classify_topic(combined_text)
                item.industry = classify_industry(combined_text)
                is_new = self.database.upsert_item(item)
                if is_new:
                    report.new_items += 1
                self.database.update_item_analysis(
                    item.id,
                    score=score,
                    breakdown=breakdown,
                    topic=item.topic,
                    industry=item.industry,
                    status=item.status if safety_findings else "scored",
                )

            # Re-score retained items when ranking rules evolve. This also prevents
            # an old aggregator entry from staying permanently over-ranked.
            for stored_item in self.database.list_items(limit=10_000):
                score, breakdown = score_item(stored_item, self.settings.raw["scoring"])
                combined_text = f"{stored_item.title} {stored_item.abstract}".lower()
                safety_findings = scan_untrusted_text(stored_item.title, stored_item.abstract)
                if safety_findings:
                    score = 0
                    breakdown["safety_quarantine"] = 100.0
                self.database.update_item_analysis(
                    stored_item.id,
                    score=score,
                    breakdown=breakdown,
                    topic=classify_topic(combined_text),
                    industry=classify_industry(combined_text),
                    status="quarantined" if safety_findings else "scored",
                )

            top_limit = int(self.settings.raw["research"].get("top_items_per_run", 5))
            platform_count = max(1, len(self.settings.raw["drafting"].get("platforms", ["linkedin"])))
            pending_limit = int(self.settings.raw["drafting"].get("max_pending_drafts", 10))
            available_item_slots = max(
                0,
                min(top_limit, (pending_limit - self.database.pending_draft_count()) // platform_count),
            )
            drafted_item_ids = self.database.drafted_item_ids()
            candidates = [
                item
                for item in self.database.list_items(limit=100, minimum_score=25)
                if item.id not in drafted_item_ids
            ]
            # AI is only spent when there is room in the approval queue.
            router = build_router(self.settings.raw) if available_item_slots > 0 else None
            judge_reasons: dict[str, str] = {}
            if router:
                report.ai_providers = router.names
                candidates, judge_reasons = self._ai_rank(router, candidates, report)

            top_items = self._select_diverse_items(candidates, available_item_slots)
            provider = choose_provider(self.settings.raw["drafting"])
            drafting = self.settings.raw["drafting"]
            platforms = drafting.get("platforms", ["linkedin"])
            ai_only = bool(drafting.get("ai_only", True))
            for item in top_items:
                is_new_brief = self.database.get_brief_for_item(item.id) is None
                document, text_source, ai_result = "", "abstract_only", None
                if router:
                    document, text_source = fetch_full_text(
                        item, user_agent=str(self.settings.raw["research"].get("user_agent", "FrontierBrandAgent/0.2"))
                    )
                    ai_result = ai_brief(router, item, document, text_source)
                if ai_result:
                    brief, claims = ai_result
                    report.ai_briefs += 1
                else:
                    brief, claims = build_brief(item), extract_claims(item)
                self.database.save_brief(brief)
                for claim in claims:
                    self.database.save_claim(claim)
                self.database.save_mission(build_mission(item, brief))
                if is_new_brief:
                    report.briefs += 1

                drafted = DraftResult()
                if router and ai_result:
                    drafted = ai_drafts(
                        router,
                        item,
                        brief,
                        claims,
                        self.settings.profile,
                        document,
                        platforms=platforms,
                        linkedin_max=int(drafting.get("linkedin_max_characters", 2600)),
                        x_max=int(drafting.get("x_max_characters", 270)),
                        x_include_link=x_include_link(self.settings),
                    )
                for platform in platforms:
                    if platform in drafted.bodies:
                        note = f"Brief from {text_source.replace('_', ' ')}; claims quote-verified; numbers checked against source."
                        if judge_reasons.get(item.id):
                            note += f" AI judge: {judge_reasons[item.id]}"
                        draft = build_draft(
                            item=item,
                            brief=brief,
                            platform=platform,
                            body=drafted.bodies[platform],
                            provider_name=router.last_client_name if router else "ai",
                            note=note,
                        )
                        if drafted.image:  # same doodle/card on LinkedIn and X
                            draft.card_json = json.dumps(drafted.image, ensure_ascii=False)
                        report.ai_drafts += 1
                    elif router and ai_only:
                        # AI is configured but could not write this one (e.g. Gemini overloaded).
                        # Skip instead of saving a weak template: the topic stays undrafted
                        # and is retried by the next run.
                        report.ai_skipped += 1
                        continue
                    else:
                        draft = create_draft(
                            item=item,
                            brief=brief,
                            platform=platform,
                            provider=provider,
                            profile=self.settings.profile,
                        )
                    self.database.save_draft(draft)
                    report.drafts += 1

            if router:
                report.ai_calls = router.calls
                if router.failures:
                    report.errors.append("AI fallback used: " + " | ".join(router.failures[:3])[:600])

            # Older pending drafts created before the mission feature are backfilled.
            for draft in self.database.list_drafts(status="pending_approval", limit=100):
                brief = self.database.get_brief(draft.brief_id)
                if brief:
                    item = self.database.get_item(brief.item_id)
                    if item:
                        self.database.save_mission(build_mission(item, brief))

            report.status = "completed_with_warnings" if report.errors else "completed"
        except Exception as exc:
            LOGGER.exception("Pipeline failed")
            report.errors.append(f"pipeline: {type(exc).__name__}: {exc}")
            report.status = "failed"
        finally:
            self.database.finish_run(
                run_id,
                status=report.status,
                discovered_count=report.discovered,
                brief_count=report.briefs,
                draft_count=report.drafts,
                errors=report.errors,
            )
        return report

    def _ai_rank(
        self,
        router: LLMRouter,
        candidates: list[ResearchItem],
        report: PipelineReport,
    ) -> tuple[list[ResearchItem], dict[str, str]]:
        """Re-ranks the keyword shortlist with the AI judge (35% keywords, 65% AI)."""
        shortlist = candidates[:JUDGE_SHORTLIST]
        try:
            ratings = judge_items(router, shortlist, self.settings.profile)
        except LLMError as exc:
            report.errors.append(f"AI judge unavailable, keyword ranking used: {str(exc)[:300]}")
            return candidates, {}
        reasons: dict[str, str] = {}
        for item in shortlist:
            rating = ratings.get(item.id)
            if not rating:
                continue
            item.score_breakdown["ai_judge"] = rating["score"]
            item.score = round(0.35 * item.score + 0.65 * rating["score"], 1)
            reasons[item.id] = rating["reason"]
            self.database.update_item_analysis(
                item.id,
                score=item.score,
                breakdown=item.score_breakdown,
                topic=item.topic,
                industry=item.industry,
                status="scored",
            )
        report.ai_judged = len(ratings)
        ranked = sorted(shortlist, key=lambda item: item.score, reverse=True)
        return ranked + candidates[JUDGE_SHORTLIST:], reasons

    def ai_preview(self) -> dict[str, Any]:
        """Runs judge -> full-text brief -> drafts on one candidate WITHOUT
        writing anything to the database. Used to verify the AI setup."""
        router = build_router(self.settings.raw)
        if not router:
            return {"ok": False, "error": "No AI provider configured. Set GEMINI_API_KEY."}
        drafted = self.database.drafted_item_ids()
        stored = self.database.list_items(limit=100, minimum_score=25)
        candidates = [item for item in stored if item.id not in drafted][:JUDGE_SHORTLIST] or stored[:JUDGE_SHORTLIST]
        if not candidates:
            return {"ok": False, "error": "No research items in the database yet. Run the pipeline first."}
        output: dict[str, Any] = {"providers": router.names}
        try:
            ratings = judge_items(router, candidates, self.settings.profile)
        except LLMError as exc:
            return {"ok": False, **output, "error": f"AI judge failed: {exc}"}
        best = max(candidates, key=lambda item: ratings.get(item.id, {}).get("score", -1))
        document, text_source = fetch_full_text(best)
        output.update(
            judged=len(ratings),
            item={"title": best.title, "url": best.url, "keyword_score": best.score},
            judge=ratings.get(best.id),
            text_source=text_source,
            text_chars=len(document),
        )
        result = ai_brief(router, best, document, text_source)
        if result:
            brief, claims = result
            output["brief"] = {
                "problem": brief.problem,
                "contribution": brief.contribution,
                "evidence": brief.evidence,
                "limitations": brief.limitations,
            }
            output["verified_claims"] = [claim.claim_text for claim in claims]
            drafted = ai_drafts(
                router,
                best,
                brief,
                claims,
                self.settings.profile,
                document,
                platforms=self.settings.raw["drafting"].get("platforms", ["linkedin"]),
            )
            output["drafts"] = drafted.bodies
            output["card"] = drafted.card
            output["doodle"] = drafted.doodle
        else:
            output["brief"] = None
        output.update(
            ok=bool(result) and bool(output.get("drafts")),
            model_used=router.last_client_name,
            calls=router.calls,
            failures=router.failures,
        )
        return output

    @staticmethod
    def _select_diverse_items(items, limit: int):
        scholarly_types = {"conference", "journal", "peer_reviewed", "curated_peer_reviewed", "preprint", "scholarly_index"}
        selected = []
        selected_ids: set[str] = set()
        source_counts: dict[str, int] = {}
        topic_counts: dict[str, int] = {}

        def can_add(item) -> bool:
            normalized_title = item.title.lower().replace("�", "'").replace("’", "'")
            if any(
                phrase in normalized_title
                for phrase in ("what's new", "weekly roundup", "monthly roundup", "year in review")
            ):
                return False
            return (
                item.id not in selected_ids
                and source_counts.get(item.source_name, 0) < 2
                and topic_counts.get(item.topic, 0) < 2
            )

        def add(item) -> None:
            selected.append(item)
            selected_ids.add(item.id)
            source_counts[item.source_name] = source_counts.get(item.source_name, 0) + 1
            topic_counts[item.topic] = topic_counts.get(item.topic, 0) + 1

        # A research-led brand needs a scholarly core, not five vendor posts.
        for item in items:
            if len(selected) >= min(4, limit):
                break
            if item.source_type in scholarly_types and can_add(item):
                add(item)

        for item in items:
            if len(selected) >= limit:
                break
            if can_add(item):
                add(item)

        return selected

