from brand_agent.models import ResearchItem
from brand_agent.scoring import classify_industry, classify_topic, score_item


SETTINGS = {
    "weights": {
        "relevance": 0.25,
        "credibility": 0.20,
        "industry_value": 0.20,
        "novelty": 0.15,
        "founder_value": 0.10,
        "actionability": 0.10,
    },
    "preferred_terms": {
        "ai": ["artificial intelligence", "agent", "inference", "large language model"],
        "cloud": ["cloud", "infrastructure", "sre", "model serving", "gpu"],
        "production": ["production", "reliability", "latency", "cost", "industry"],
        "founder": ["product", "customer", "business", "adoption"],
        "actionability": ["benchmark", "dataset", "open source", "implementation"],
    },
}


def item(title: str, abstract: str) -> ResearchItem:
    return ResearchItem(
        id="item_test",
        title=title,
        abstract=abstract,
        url="https://example.com",
        source_name="Test conference",
        source_type="peer_reviewed",
        publication_status="peer_reviewed",
        published_at="2026-10-01T00:00:00+00:00",
        content_hash="hash",
    )


def test_cloud_ai_production_item_scores_above_unrelated_item():
    relevant = item(
        "AI agents for cloud incident response",
        "A production benchmark evaluates agent reliability for SRE infrastructure with cost and latency measurements.",
    )
    unrelated = item("Image color preferences", "A study of color names in historical fiction.")
    relevant_score, _ = score_item(relevant, SETTINGS)
    unrelated_score, _ = score_item(unrelated, SETTINGS)
    assert relevant_score > unrelated_score + 20


def test_topic_and_industry_classification():
    text = "an aiops agent performs sre incident root cause analysis for developer platforms"
    assert classify_topic(text) == "ai_for_cloud"
    assert classify_industry(text) == "developer_platforms"

