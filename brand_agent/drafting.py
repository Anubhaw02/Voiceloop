from __future__ import annotations

import json
import logging
import textwrap
import uuid
from typing import Any, Protocol

import httpx

from .models import ContentDraft, ResearchBrief, ResearchItem


LOGGER = logging.getLogger(__name__)
NAMESPACE = uuid.UUID("bb21b62c-d6fa-4661-b6df-aa9bcf91169d")


class DraftProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def draft(
        self,
        *,
        item: ResearchItem,
        brief: ResearchBrief,
        platform: str,
        profile: dict[str, Any],
    ) -> str: ...


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        try:
            response = httpx.get(f"{self.base_url}/api/tags", timeout=2)
            response.raise_for_status()
            models = [model.get("name", "") for model in response.json().get("models", [])]
            return any(name == self.model or name.startswith(f"{self.model}:") for name in models)
        except Exception:
            return False

    def draft(
        self,
        *,
        item: ResearchItem,
        brief: ResearchBrief,
        platform: str,
        profile: dict[str, Any],
    ) -> str:
        maximum = 2500 if platform == "linkedin" else 260
        prompt = f"""
Write a {platform} post of at most {maximum} characters.

Identity: {profile['identity']['positioning']}
Voice: quietly confident, clear, educational, technically grounded, slightly bold only when evidence supports it.

Non-negotiable rules:
- Never invent personal experience, expertise, results, or opinions.
- Attribute every research result to the source.
- Clearly label interpretation as interpretation.
- Mention the publication status: {item.publication_status}.
- Do not use hype, fake vulnerability, or generic motivational language.
- Include this source URL exactly once: {item.url}

Research title: {item.title}
Reported problem: {brief.problem}
Reported contribution: {brief.contribution}
Reported evidence: {brief.evidence}
Limitations: {brief.limitations}
Cloud implications: {brief.cloud_implications}
Industry implications: {brief.industry_implications}

Return only the post text.
""".strip()
        response = httpx.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": prompt, "stream": False},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()["response"].strip()


class TemplateProvider:
    name = "evidence_first_template"

    def available(self) -> bool:
        return True

    @staticmethod
    def _hook(item: ResearchItem) -> str:
        hooks = {
            "ai_for_cloud": "The next step in cloud operations may not be another dashboard. It may be an AI system that can investigate incidents—inside carefully designed boundaries.",
            "cloud_for_ai": "The model gets the attention. The infrastructure decides whether the AI product is fast, reliable, and affordable enough to survive.",
            "ai_in_industry": "The interesting AI question is no longer only what a model can do. It is whether that capability survives contact with a real industry workflow.",
            "agent_reliability": "An agent completing a task once is a demo. Completing it consistently, under failures and changing conditions, is an engineering system.",
        }
        return hooks.get(
            item.topic,
            "The most valuable AI research is often the work that changes how real systems are built.",
        )

    def draft(
        self,
        *,
        item: ResearchItem,
        brief: ResearchBrief,
        platform: str,
        profile: dict[str, Any],
    ) -> str:
        if platform == "x":
            maximum = 270
            suffix = f"\n\nProduction question: cost, reliability and security?\n{item.url}"
            title_budget = max(18, min(62, maximum - len(suffix) - 55))
            prefix = f"Research signal: {item.title[:title_budget]}\n\n"
            available = max(0, maximum - len(prefix) - len(suffix))
            evidence = brief.evidence[:available].rstrip(" ,;:-")
            body = f"{prefix}{evidence}{suffix}"
            if len(body) > maximum:
                prefix = "Research signal:\n\n"
                available = max(0, maximum - len(prefix) - len(suffix))
                body = f"{prefix}{brief.evidence[:available].rstrip(' ,;:-')}{suffix}"
            return body

        status_label = item.publication_status.replace("_", " ")
        body = f"""
{self._hook(item)}

I came across **{item.title}** while exploring the intersection of cloud infrastructure, AI, and real-world deployment.

The source describes this problem:

{brief.problem}

Its reported contribution is:

{brief.contribution}

One result worth examining:

{brief.evidence}

My current interpretation:

{brief.cloud_implications}

But this is where technical excitement needs discipline.

{brief.limitations}

The real opportunity is not simply to repeat the result. It is to test whether it creates a reliable, affordable outcome for {item.industry.replace('_', ' ')}.

The question I would ask before building on it:

**What assumptions break first when this moves from the paper into production?**

Publication status: {status_label}.
Source: {item.url}
""".strip()
        return textwrap.dedent(body)


def choose_provider(config: dict[str, Any]) -> DraftProvider:
    requested = config.get("provider", "auto")
    ollama = OllamaProvider(
        base_url=config.get("ollama_url", "http://127.0.0.1:11434"),
        model=config.get("ollama_model", "qwen2.5:7b"),
    )
    if requested in {"auto", "ollama"} and ollama.available():
        LOGGER.info("Using local Ollama model %s", ollama.model)
        return ollama
    if requested == "ollama":
        LOGGER.warning("Configured Ollama model is unavailable; using safe template provider")
    return TemplateProvider()


def create_draft(
    *,
    item: ResearchItem,
    brief: ResearchBrief,
    platform: str,
    provider: DraftProvider,
    profile: dict[str, Any],
) -> ContentDraft:
    body = provider.draft(item=item, brief=brief, platform=platform, profile=profile)
    return build_draft(item=item, brief=brief, platform=platform, body=body, provider_name=provider.name)


def build_draft(
    *,
    item: ResearchItem,
    brief: ResearchBrief,
    platform: str,
    body: str,
    provider_name: str,
    note: str = "",
) -> ContentDraft:
    draft_id = f"draft_{uuid.uuid5(NAMESPACE, brief.id + ':' + platform).hex[:20]}"
    risk = "medium" if item.publication_status.startswith("preprint") else "low"
    return ContentDraft(
        id=draft_id,
        brief_id=brief.id,
        platform=platform,
        title=f"{item.topic.replace('_', ' ').title()}: {item.title}",
        body=body,
        rationale=(
            f"Selected for Cloud x AI relevance ({item.score:.1f}/100), "
            f"{item.publication_status.replace('_', ' ')}, and potential value for {item.industry.replace('_', ' ')}. "
            f"Drafted with {provider_name}."
            + (f" {note}" if note else "")
        ),
        risk_level=risk,
    )

