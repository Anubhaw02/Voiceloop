from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import feedparser
import httpx

from .models import ResearchItem
from .scoring import clean_text


LOGGER = logging.getLogger(__name__)


def _hash_title(title: str) -> str:
    normalized = " ".join(title.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _id_for(source_name: str, title: str) -> str:
    digest = hashlib.sha256(f"{source_name}:{title}".encode("utf-8")).hexdigest()[:20]
    return f"item_{digest}"


def _safe_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return value


def _reconstruct_openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, indexes in inverted.items():
        positions.extend((index, word) for index in indexes)
    positions.sort(key=lambda value: value[0])
    return clean_text(" ".join(word for _, word in positions))


class ResearchSource(Protocol):
    name: str

    def discover(self) -> list[ResearchItem]: ...


@dataclass(slots=True)
class DiscoveryResult:
    items: list[ResearchItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class BaseHTTPSource:
    def __init__(self, *, timeout: float, user_agent: str):
        self.client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "application/json, application/atom+xml, application/xml, text/xml, */*"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()


class ArxivSource(BaseHTTPSource):
    name = "arXiv"
    endpoint = "https://export.arxiv.org/api/query"

    def __init__(
        self,
        queries: list[str],
        *,
        max_results: int,
        timeout: float,
        user_agent: str,
    ):
        super().__init__(timeout=timeout, user_agent=user_agent)
        self.queries = queries
        self.max_results = max_results

    def discover(self) -> list[ResearchItem]:
        items: list[ResearchItem] = []
        for query in self.queries:
            response = self.client.get(
                self.endpoint,
                params={
                    "search_query": query,
                    "start": 0,
                    "max_results": self.max_results,
                    "sortBy": "submittedDate",
                    "sortOrder": "descending",
                },
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries:
                title = clean_text(entry.get("title", ""))
                abstract = clean_text(entry.get("summary", ""))
                if not title or not abstract:
                    continue
                url = entry.get("link", "")
                authors = [author.get("name", "") for author in entry.get("authors", [])]
                items.append(
                    ResearchItem(
                        id=_id_for(self.name, title),
                        title=title,
                        abstract=abstract,
                        url=url,
                        source_name=self.name,
                        source_type="preprint",
                        publication_status="preprint_not_peer_reviewed",
                        published_at=_safe_date(entry.get("published")),
                        authors=[author for author in authors if author],
                        raw={"query": query, "arxiv_id": entry.get("id", "")},
                        content_hash=_hash_title(title),
                    )
                )
        return items


class OpenAlexSource(BaseHTTPSource):
    name = "OpenAlex"
    endpoint = "https://api.openalex.org/works"

    def __init__(
        self,
        queries: list[str],
        *,
        lookback_days: int,
        max_results: int,
        timeout: float,
        user_agent: str,
    ):
        super().__init__(timeout=timeout, user_agent=user_agent)
        self.queries = queries
        self.lookback_days = lookback_days
        self.max_results = max_results

    def discover(self) -> list[ResearchItem]:
        items: list[ResearchItem] = []
        from_date = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).date()
        api_key = os.getenv("OPENALEX_API_KEY", "").strip()
        for query in self.queries:
            params: dict[str, Any] = {
                "search": query,
                "filter": f"from_publication_date:{from_date.isoformat()},is_retracted:false",
                "sort": "publication_date:desc",
                "per-page": self.max_results,
                "select": "id,doi,title,publication_date,type,authorships,abstract_inverted_index,primary_location,open_access",
            }
            if api_key:
                params["api_key"] = api_key
            response = self.client.get(self.endpoint, params=params)
            if response.status_code in (401, 403, 429):
                hint = "" if api_key else " OpenAlex requires a free API key: set OPENALEX_API_KEY."
                # Never echo the request URL: it would contain the key.
                raise RuntimeError(f"OpenAlex returned HTTP {response.status_code}.{hint}")
            if response.status_code >= 400:
                raise RuntimeError(f"OpenAlex returned HTTP {response.status_code}.")
            for work in response.json().get("results", []):
                title = clean_text(work.get("title", ""))
                abstract = _reconstruct_openalex_abstract(work.get("abstract_inverted_index"))
                if not title or not abstract:
                    continue
                location = work.get("primary_location") or {}
                source = location.get("source") or {}
                url = work.get("doi") or location.get("landing_page_url") or work.get("id", "")
                authors = [
                    authorship.get("author", {}).get("display_name", "")
                    for authorship in work.get("authorships", [])
                ]
                items.append(
                    ResearchItem(
                        id=_id_for(self.name, title),
                        title=title,
                        abstract=abstract,
                        url=url,
                        source_name=source.get("display_name") or self.name,
                        source_type="scholarly_index",
                        publication_status="indexed_work_verify_peer_review_status",
                        published_at=_safe_date(work.get("publication_date")),
                        authors=[author for author in authors if author],
                        raw={
                            "query": query,
                            "openalex_id": work.get("id"),
                            "work_type": work.get("type"),
                            "is_oa": (work.get("open_access") or {}).get("is_oa"),
                        },
                        content_hash=_hash_title(title),
                    )
                )
        return items


class RSSSource(BaseHTTPSource):
    def __init__(
        self,
        *,
        name: str,
        url: str,
        source_type: str,
        lookback_days: int,
        timeout: float,
        user_agent: str,
    ):
        super().__init__(timeout=timeout, user_agent=user_agent)
        self.name = name
        self.url = url
        self.source_type = source_type
        self.lookback_days = lookback_days

    def discover(self) -> list[ResearchItem]:
        response = self.client.get(self.url)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        items: list[ResearchItem] = []
        for entry in feed.entries:
            title = clean_text(entry.get("title", ""))
            abstract = clean_text(
                entry.get("summary", "") or entry.get("description", "") or entry.get("subtitle", "")
            )
            abstract = abstract[:5000]
            if not title or not abstract:
                continue
            lowered_title = title.lower()
            if any(
                phrase in lowered_title
                for phrase in ("what's new", "what’s new", "weekly roundup", "monthly roundup")
            ):
                continue
            published = entry.get("published") or entry.get("updated")
            parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            if parsed:
                published_dt = datetime(*parsed[:6], tzinfo=timezone.utc)
                if published_dt < cutoff:
                    continue
                published = published_dt.isoformat()
            url = entry.get("link", "")
            domain = urlparse(url).netloc
            items.append(
                ResearchItem(
                    id=_id_for(self.name, title),
                    title=title,
                    abstract=abstract,
                    url=url,
                    source_name=self.name,
                    source_type=self.source_type,
                    publication_status="official_source_not_independent_peer_review",
                    published_at=_safe_date(published),
                    authors=[entry.get("author", "")] if entry.get("author") else [],
                    raw={"feed": self.url, "domain": domain},
                    content_hash=_hash_title(title),
                )
            )
        return items


class StaticJSONSource:
    name = "Curated local seed"

    def __init__(self, path: Path):
        self.path = path

    def discover(self) -> list[ResearchItem]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        items: list[ResearchItem] = []
        for record in payload:
            title = clean_text(record["title"])
            record.setdefault("content_hash", _hash_title(title))
            record.setdefault("id", _id_for(record.get("source_name", self.name), title))
            items.append(ResearchItem(**record))
        return items


def build_sources(config: dict[str, Any], project_root: Path) -> list[ResearchSource]:
    timeout = float(config.get("request_timeout_seconds", 25))
    user_agent = str(config.get("user_agent", "FrontierBrandAgent/0.1"))
    max_results = int(config.get("items_per_query", 10))
    lookback = int(config.get("lookback_days", 45))

    sources: list[ResearchSource] = [StaticJSONSource(project_root / "data" / "sample_items.json")]
    sources.append(
        ArxivSource(
            list(config.get("arxiv_queries", [])),
            max_results=max_results,
            timeout=timeout,
            user_agent=user_agent,
        )
    )
    sources.append(
        OpenAlexSource(
            list(config.get("openalex_queries", [])),
            lookback_days=lookback,
            max_results=max_results,
            timeout=timeout,
            user_agent=user_agent,
        )
    )
    for feed in config.get("rss_feeds", []):
        sources.append(
            RSSSource(
                name=feed["name"],
                url=feed["url"],
                source_type=feed.get("source_type", "official_engineering"),
                lookback_days=lookback,
                timeout=timeout,
                user_agent=user_agent,
            )
        )
    return sources


def discover_all(sources: list[ResearchSource]) -> DiscoveryResult:
    result = DiscoveryResult()
    seen_hashes: set[str] = set()
    for source in sources:
        try:
            for item in source.discover():
                if item.content_hash in seen_hashes:
                    continue
                seen_hashes.add(item.content_hash)
                result.items.append(item)
        except Exception as exc:  # A single source must never stop the pipeline.
            message = f"{source.name}: {type(exc).__name__}: {exc}"
            LOGGER.warning("Source discovery failed: %s", message)
            result.errors.append(message)
        finally:
            close = getattr(source, "close", None)
            if callable(close):
                close()
    return result

