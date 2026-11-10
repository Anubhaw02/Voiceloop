from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from .config import Settings
from .db import Database
from .drafting import choose_provider, create_draft
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
    errors: list[str] = field(default_factory=list)


class ResearchPipeline:
    def __init__(self, settings: Settings, database: Database | None = None):
        self.settings = settings
        self.database = database or Database(settings.database_path)
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
            top_items = self._select_diverse_items(candidates, available_item_slots)
            provider = choose_provider(self.settings.raw["drafting"])
            for item in top_items:
                brief = self.database.get_brief_for_item(item.id)
                is_new_brief = brief is None
                brief = build_brief(item)
                self.database.save_brief(brief)
                for claim in extract_claims(item):
                    self.database.save_claim(claim)
                self.database.save_mission(build_mission(item, brief))
                if is_new_brief:
                    report.briefs += 1

                for platform in self.settings.raw["drafting"].get("platforms", ["linkedin"]):
                    draft = create_draft(
                        item=item,
                        brief=brief,
                        platform=platform,
                        provider=provider,
                        profile=self.settings.profile,
                    )
                    self.database.save_draft(draft)
                    report.drafts += 1

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

