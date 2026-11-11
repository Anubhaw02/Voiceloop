"""Fetches the full text of a research item so the model reads the paper,
not just the abstract. Everything returned here is untrusted input."""
from __future__ import annotations

import io
import logging
import re
from html import unescape

import httpx

from .models import ResearchItem
from .safety import scan_untrusted_text


LOGGER = logging.getLogger(__name__)
MAX_CHARS = 150_000
MIN_USEFUL_CHARS = 2_500
BLOCKING_RULES = {"instruction_override", "credential_exfiltration"}
ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf|html)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", re.I)


def html_to_text(html: str) -> str:
    article = re.search(r"(?is)<article\b.*</article>", html)
    if article:  # arXiv HTML papers and most blogs wrap the content in <article>
        html = article.group(0)
    html = re.sub(r"(?is)<(script|style|nav|header|footer|svg|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|li|tr|section)>", "\n", html)
    text = unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def pdf_to_text(data: bytes, max_pages: int = 30) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages[:max_pages]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # a single broken page must not lose the paper
            continue
    return re.sub(r"[ \t]+", " ", "\n".join(pages)).strip()


def _candidate_urls(item: ResearchItem) -> list[str]:
    match = ARXIV_ID.search(item.url) or ARXIV_ID.search(str(item.raw.get("arxiv_id", "")))
    if match:
        paper = match.group(1)
        return [f"https://arxiv.org/html/{paper}", f"https://arxiv.org/pdf/{paper}"]
    return [item.url] if item.url.startswith("http") else []


def fetch_full_text(item: ResearchItem, *, timeout: float = 40, user_agent: str = "FrontierBrandAgent/0.2") -> tuple[str, str]:
    """Returns (text, label). Falls back to the abstract when the full text is
    unavailable, too short (paywall/landing page), or looks like an injection."""
    headers = {"User-Agent": user_agent}
    for url in _candidate_urls(item):
        try:
            response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
            if response.status_code >= 400:
                continue
            content_type = response.headers.get("content-type", "").lower()
            if "pdf" in content_type or response.content[:5] == b"%PDF-":
                text = pdf_to_text(response.content)
            elif "html" in content_type or "xml" in content_type or "text" in content_type:
                text = html_to_text(response.text)
            else:
                continue
        except Exception as exc:
            LOGGER.info("Full text unavailable from %s: %s", url, type(exc).__name__)
            continue
        if len(text) < MIN_USEFUL_CHARS:
            continue
        # Papers about ops/agents naturally say "run the tool" or "execute a command",
        # so only manipulation patterns disqualify a full text. The model has no
        # tools and its output is verified by code, so these two rules suffice.
        manipulation = [f for f in scan_untrusted_text(text) if f.rule in BLOCKING_RULES]
        if manipulation:
            LOGGER.warning("Full text for %s contains %s; using abstract only", item.id, manipulation[0].rule)
            break
        return text[:MAX_CHARS], "full_text"
    return item.abstract, "abstract_only"
