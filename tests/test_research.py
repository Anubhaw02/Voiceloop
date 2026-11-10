from brand_agent.models import ResearchItem
from brand_agent.research import build_brief, build_mission, extract_claims
from brand_agent.drafting import TemplateProvider


def test_brief_and_claims_preserve_source_caution():
    item = ResearchItem(
        id="item_preprint",
        title="An agent benchmark",
        abstract=(
            "Cloud incident response remains difficult in production. "
            "We introduce a benchmark for agent-based root cause analysis. "
            "Evaluation on 100 incidents improves localization accuracy by 20%."
        ),
        url="https://arxiv.org/abs/0000.0000",
        source_name="arXiv",
        source_type="preprint",
        publication_status="preprint_not_peer_reviewed",
        content_hash="hash_preprint",
        topic="ai_for_cloud",
        industry="developer_platforms",
    )
    brief = build_brief(item)
    claims = extract_claims(item)
    assert "preprint" in brief.limitations.lower()
    assert any("20%" in claim.claim_text for claim in claims)
    assert all("not_independently_reproduced" in claim.verification_status for claim in claims)


def test_x_draft_keeps_source_link_inside_limit():
    item = ResearchItem(
        id="item_x",
        title="A very long research title about distributed cloud inference systems and production reliability",
        abstract="A production system evaluates cloud AI reliability.",
        url=(
            "https://example.com/research/a-very-long-but-important-source-link/"
            "with-several-path-components-that-must-never-be-truncated"
        ),
        source_name="Conference",
        source_type="peer_reviewed",
        content_hash="hash_x",
        topic="cloud_for_ai",
        industry="developer_platforms",
    )
    brief = build_brief(item)
    body = TemplateProvider().draft(item=item, brief=brief, platform="x", profile={})
    assert len(body) <= 270
    assert item.url in body


def test_mission_connects_research_to_real_evidence():
    item = ResearchItem(
        id="item_mission",
        title="AI agents for cloud incidents",
        abstract="We evaluate agents for cloud incident response.",
        url="https://example.com/incident",
        source_name="Conference",
        source_type="peer_reviewed",
        content_hash="hash_mission",
        topic="ai_for_cloud",
        industry="developer_platforms",
    )
    mission = build_mission(item, build_brief(item))
    assert "incident workflow" in mission.objective
    assert "own words" in mission.evidence_required
    assert mission.status == "proposed"

