from pathlib import Path
from typing import Any

import pytest

from brand_agent import pipeline as pipeline_module
from brand_agent.ai_research import (
    ai_brief,
    ai_drafts,
    forbidden_phrases,
    judge_items,
    quote_in_source,
    unsupported_numbers,
)
from brand_agent.config import Settings
from brand_agent.db import Database
from brand_agent.llm import GeminiClient, LLMError, LLMRouter, build_router
from brand_agent.models import ResearchItem


ABSTRACT = (
    "Serving large language models in the cloud is expensive because GPU memory is underused. "
    "We present KVShare, a cache-sharing scheduler for multi-tenant inference clusters. "
    "On production traces, KVShare reduces p99 latency by 37% and improves GPU utilisation 2.1x over vLLM. "
    "We evaluate across 3 cloud regions and 1,200 tenants with real enterprise workloads."
)


def make_item(item_id: str = "item_ai", score: float = 60.0) -> ResearchItem:
    return ResearchItem(
        id=item_id,
        title=f"KVShare {item_id}",
        abstract=ABSTRACT,
        url=f"https://arxiv.org/abs/2611.0{len(item_id):04d}",
        source_name="arXiv",
        source_type="preprint",
        publication_status="preprint_not_peer_reviewed",
        content_hash=f"hash_{item_id}",
        score=score,
        topic="cloud_for_ai",
        industry="enterprise_software",
    )


class FakeClient:
    """Returns queued JSON responses in order, or raises LLMError."""

    max_document_chars = 50_000

    def __init__(self, responses: list[Any], name: str = "fake"):
        self.responses = list(responses)
        self.name = name
        self.prompts: list[str] = []

    def generate_json(self, *, system: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


GOOD_BRIEF = {
    "problem": "GPU memory in multi-tenant inference clusters is underused, raising serving cost.",
    "contribution": "KVShare shares KV caches across tenants with a new scheduler.",
    "evidence": "Production traces show 37% lower p99 latency and 2.1x GPU utilisation versus vLLM.",
    "limitations": "Preprint; tested on 3 regions only.",
    "cloud_implications": "Lower serving cost per tenant.",
    "industry_implications": "Enterprise SaaS running shared inference benefits.",
    "founder_questions": ["Does it hold under bursty traffic?"],
    "claims": [
        {"claim": "Lower tail latency", "quote": "On production traces, KVShare reduces p99 latency by 37%"},
        {"claim": "Real workloads", "quote": "We evaluate across 3 cloud regions and 1,200 tenants with real enterprise workloads."},
        {"claim": "Invented", "quote": "KVShare eliminates all GPU cost for every customer worldwide."},
    ],
}

LINKEDIN = (
    "Most inference cost discussions focus on the model. This preprint argues the scheduler matters as much.\n\n"
    "The authors present KVShare, which shares KV caches across tenants in multi-tenant inference clusters. "
    "On production traces they report 37% lower p99 latency and 2.1x higher GPU utilisation than vLLM, "
    "across 3 cloud regions.\n\n"
    "My read: for enterprise SaaS teams paying for idle GPU memory, cache sharing could be a cheaper lever "
    "than buying more accelerators. The caveat: this is a preprint, not yet peer reviewed, and the "
    "evaluation covers a limited set of regions.\n\n"
    "Founders running shared inference: would you trade tenant isolation for this efficiency?"
)


def test_quote_and_number_verification():
    assert quote_in_source("KVShare reduces p99 latency by 37% and improves", ABSTRACT)
    assert not quote_in_source("KVShare eliminates all GPU cost for every customer", ABSTRACT)
    assert not quote_in_source("reduces p99", ABSTRACT)  # too short to count as a quote
    assert unsupported_numbers("37% lower latency, 2.1x utilisation, 1200 tenants in 2026", ABSTRACT) == []
    assert unsupported_numbers("cuts cost by 80%", ABSTRACT) == ["80%"]
    assert forbidden_phrases("At my company we built this") == ["At my company", "we built"]
    assert forbidden_phrases("I worked on this at Bidwiser") == ["Bidwiser"]


def test_ai_brief_keeps_only_quote_verified_claims():
    router = LLMRouter([FakeClient([GOOD_BRIEF])])
    result = ai_brief(router, make_item(), ABSTRACT, "full_text")
    assert result is not None
    brief, claims = result
    assert brief.problem.startswith("GPU memory")
    assert len(claims) == 2
    assert all(claim.verification_status == "quote_verified_in_source" for claim in claims)


def test_ai_brief_rejected_without_enough_verified_claims():
    bad = dict(GOOD_BRIEF, claims=[{"claim": "x", "quote": "Completely invented sentence that is not in the paper."}])
    assert ai_brief(LLMRouter([FakeClient([bad])]), make_item(), ABSTRACT, "full_text") is None


def test_ai_drafts_retry_on_invented_number_then_accept():
    item = make_item()
    router = LLMRouter([FakeClient([GOOD_BRIEF])])
    brief, claims = ai_brief(router, item, ABSTRACT, "full_text")
    bad = {"linkedin": LINKEDIN.replace("37%", "80%"), "x": "KVShare: 37% lower p99 latency vs vLLM on production traces."}
    good = {"linkedin": LINKEDIN, "x": "KVShare: 37% lower p99 latency vs vLLM on production traces."}
    client = FakeClient([bad, good])
    drafts = ai_drafts(LLMRouter([client]), item, brief, claims, {}, ABSTRACT, platforms=["linkedin", "x"])
    assert set(drafts) == {"linkedin", "x"}
    assert "80%" in client.prompts[1] and "numbers not in the source" in client.prompts[1]
    assert drafts["linkedin"].endswith(f"Source: {item.url}")
    assert len(drafts["x"]) <= 270


def test_router_falls_back_to_next_provider():
    first = FakeClient([LLMError("rate limited")], name="gemini")
    second = FakeClient([{"ratings": []}], name="github")
    router = LLMRouter([first, second])
    assert router.generate_json(system="s", prompt="p") == {"ratings": []}
    assert router.last_client_name == "github"


def test_judge_scores_and_penalises_hype():
    items = [make_item("item_a"), make_item("item_b")]
    ratings = {
        "ratings": [
            {"id": "item_a", "novelty": 9, "real_world_value": 9, "evidence_strength": 8, "brand_fit": 9, "hype_risk": 1, "reason": "solid"},
            {"id": "item_b", "novelty": 9, "real_world_value": 9, "evidence_strength": 8, "brand_fit": 9, "hype_risk": 9, "reason": "hype"},
            {"id": "unknown", "novelty": 10},
        ]
    }
    result = judge_items(LLMRouter([FakeClient([ratings])]), items, {})
    assert set(result) == {"item_a", "item_b"}
    assert result["item_a"]["score"] > result["item_b"]["score"]


def test_gemini_picks_newest_stable_flash_model(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            methods = ["generateContent"]
            return {
                "models": [
                    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": methods},
                    {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": methods},
                    {"name": "models/gemini-3.5-flash-preview-09", "supportedGenerationMethods": methods},
                    {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": methods},
                    {"name": "models/gemini-3.5-flash-image", "supportedGenerationMethods": methods},
                    {"name": "models/gemini-3.5-pro", "supportedGenerationMethods": methods},
                    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                ]
            }

    monkeypatch.setattr("brand_agent.llm.httpx.get", lambda *a, **k: Response())
    assert GeminiClient("key").candidate_models() == [
        "gemini-3.5-flash",
        "gemini-3.5-flash-preview-09",
        "gemini-3.5-flash-lite",
    ]


def test_build_router_requires_a_key(monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)  # CI sets this itself
    assert build_router({}) is None
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    router = build_router({})
    assert router is not None and router.names == ["gemini"]
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    assert build_router({}).names == ["gemini"]  # GitHub Models only inside Actions
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert build_router({}).names == ["gemini", "github-models"]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        raw={
            "app": {"name": "Test", "database": "data/test.db", "export_directory": "exports"},
            "safety": {"publish_enabled": False, "require_approval": True, "allow_external_writes": False},
            "research": {"top_items_per_run": 1},
            "scoring": {
                "preferred_terms": {
                    "ai": ["inference"],
                    "cloud": ["cloud"],
                    "production": ["production"],
                    "founder": ["enterprise"],
                    "actionability": ["scheduler"],
                },
                "weights": {},
            },
            "drafting": {"platforms": ["linkedin", "x"], "max_pending_drafts": 10, "provider": "template"},
            "scheduler": {},
        },
        profile={"identity": {"positioning": "builder"}},
        project_root=tmp_path,
    )


@pytest.mark.parametrize("ai_works", [True, False])
def test_pipeline_uses_ai_and_falls_back_to_templates(tmp_path: Path, monkeypatch, ai_works: bool):
    settings = _settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    item = make_item("item_pipe", score=80)
    monkeypatch.setattr(pipeline_module, "build_sources", lambda *a, **k: [])
    monkeypatch.setattr(pipeline_module, "discover_all", lambda sources: type("R", (), {"items": [item], "errors": []})())
    monkeypatch.setattr(pipeline_module, "score_item", lambda item, cfg: (80.0, {"relevance": 1.0}))
    monkeypatch.setattr(pipeline_module, "fetch_full_text", lambda item, **k: (ABSTRACT, "full_text"))
    if ai_works:
        responses: list[Any] = [
            {"ratings": [{"id": "item_pipe", "novelty": 8, "real_world_value": 8, "evidence_strength": 8, "brand_fit": 8, "hype_risk": 0, "reason": "useful"}]},
            GOOD_BRIEF,
            {"linkedin": LINKEDIN, "x": "KVShare: 37% lower p99 latency vs vLLM on production traces."},
        ]
    else:
        responses = [LLMError("quota")] * 3
    fake = FakeClient(responses, name="gemini/fake")
    monkeypatch.setattr(pipeline_module, "build_router", lambda config: LLMRouter([fake]))

    report = pipeline_module.ResearchPipeline(settings, database).run()

    assert report.status in {"completed", "completed_with_warnings"}
    drafts = database.list_drafts(limit=10)
    assert {draft.platform for draft in drafts} == {"linkedin", "x"}
    if ai_works:
        assert report.ai_judged == 1 and report.ai_briefs == 1 and report.ai_drafts == 2
        assert all("gemini/fake" in draft.rationale for draft in drafts)
        assert any(draft.body.startswith("Most inference cost") for draft in drafts)
    else:
        assert report.ai_drafts == 0
        assert all("evidence_first_template" in draft.rationale for draft in drafts)
        assert any("AI" in error for error in report.errors)
