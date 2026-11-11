"""Free-tier LLM access with ordered fallback.

The model only ever returns text (JSON). It has no tools and cannot take
actions; every output is validated by code before it is stored.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Protocol

import httpx


LOGGER = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    name: str
    max_document_chars: int

    def generate_json(self, *, system: str, prompt: str, max_output_tokens: int) -> dict[str, Any]: ...


def _parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model returned invalid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise LLMError("model returned JSON that is not an object")
    return data


def _version_key(name: str) -> tuple[float, int]:
    match = re.search(r"(\d+(?:\.\d+)?)", name)
    version = float(match.group(1)) if match else 0.0
    stable = 0 if re.search(r"preview|exp", name) else 1
    return (version, stable)


class GeminiClient:
    """Gemini API (Google AI Studio key). Model names change often, so the
    client asks the API which Flash models this key can use."""

    base_url = "https://generativelanguage.googleapis.com/v1beta"
    max_document_chars = 120_000
    _excluded = ("image", "tts", "audio", "live", "embedding", "vision", "learnlm", "robotics", "computer-use")

    retryable = {429, 500, 503}

    def __init__(self, api_key: str, *, preferred_model: str = "", min_interval_seconds: float = 6.0, timeout: float = 150):
        self._headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        self.preferred_model = preferred_model.strip().removeprefix("models/")
        self.min_interval_seconds = min_interval_seconds
        self.timeout = timeout
        self._models: list[str] | None = None
        self._last_call = 0.0
        self.model_used = ""

    @property
    def name(self) -> str:
        return f"gemini/{self.model_used}" if self.model_used else "gemini"

    def candidate_models(self) -> list[str]:
        if self._models is not None:
            return self._models
        response = httpx.get(f"{self.base_url}/models", params={"pageSize": 1000}, headers=self._headers, timeout=30)
        if response.status_code in (401, 403):
            raise LLMError(f"Gemini API key rejected (HTTP {response.status_code})")
        response.raise_for_status()
        names = [
            model["name"].removeprefix("models/")
            for model in response.json().get("models", [])
            if "generateContent" in model.get("supportedGenerationMethods", [])
        ]
        flash = [n for n in names if "flash" in n and not any(word in n for word in self._excluded)]
        full = sorted((n for n in flash if "lite" not in n), key=_version_key, reverse=True)
        lite = sorted((n for n in flash if "lite" in n), key=_version_key, reverse=True)
        ordered = full[:2] + lite[:1]
        if self.preferred_model and self.preferred_model in names:
            ordered = [self.preferred_model] + [n for n in ordered if n != self.preferred_model]
        if not ordered:
            raise LLMError("no Gemini Flash model with generateContent is available for this key")
        self._models = ordered
        LOGGER.info("Gemini model candidates: %s", ", ".join(ordered))
        return ordered

    def _pace(self) -> None:
        wait = self.min_interval_seconds - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def generate_json(self, *, system: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.3,
                "maxOutputTokens": max_output_tokens,
            },
        }
        failures: list[str] = []
        for model in self.candidate_models():
            for attempt in range(2):
                self._pace()
                try:
                    response = httpx.post(
                        f"{self.base_url}/models/{model}:generateContent",
                        headers=self._headers,
                        json=body,
                        timeout=self.timeout,
                    )
                except httpx.HTTPError as exc:
                    failures.append(f"{model}: {type(exc).__name__}")
                    break
                if response.status_code in self.retryable and attempt == 0:
                    default = "20" if response.status_code == 429 else "15"
                    try:
                        delay = min(60.0, float(response.headers.get("retry-after", default) or default))
                    except ValueError:
                        delay = float(default)
                    LOGGER.warning("Gemini %s returned HTTP %s; retrying in %.0fs", model, response.status_code, delay)
                    time.sleep(delay)
                    continue
                if response.status_code >= 400:
                    failures.append(f"{model}: HTTP {response.status_code}")
                    break
                try:
                    data = response.json()
                except ValueError:
                    failures.append(f"{model}: non-JSON response")
                    break
                candidates = data.get("candidates") or []
                if not candidates:
                    reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
                    failures.append(f"{model}: {reason}")
                    break
                parts = (candidates[0].get("content") or {}).get("parts") or []
                text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
                self.model_used = model
                return _parse_json(text)
        raise LLMError("Gemini failed: " + "; ".join(failures))


class GitHubModelsClient:
    """GitHub Models, free inside GitHub Actions via GITHUB_TOKEN (models: read)."""

    base_url = "https://models.github.ai"
    max_document_chars = 20_000  # free-tier request size is small
    preferred = ("openai/gpt-4.1-mini", "openai/gpt-4o-mini", "openai/gpt-4.1", "openai/gpt-4o")

    def __init__(self, token: str, *, timeout: float = 120):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.timeout = timeout
        self._model: str | None = None

    @property
    def name(self) -> str:
        return f"github-models/{self._model}" if self._model else "github-models"

    def _choose_model(self) -> str:
        if self._model:
            return self._model
        chosen = self.preferred[0]
        try:
            response = httpx.get(f"{self.base_url}/catalog/models", headers=self._headers, timeout=30)
            response.raise_for_status()
            available = {model.get("id") for model in response.json()}
            chosen = next((model for model in self.preferred if model in available), chosen)
        except Exception as exc:  # catalog is optional; fall back to the default id
            LOGGER.info("GitHub Models catalog unavailable (%s); using %s", type(exc).__name__, chosen)
        self._model = chosen
        return chosen

    def generate_json(self, *, system: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        model = self._choose_model()
        try:
            response = httpx.post(
                f"{self.base_url}/inference/chat/completions",
                headers=self._headers,
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.3,
                    "max_tokens": min(max_output_tokens, 4000),
                },
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"GitHub Models failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise LLMError(f"GitHub Models failed: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            content_type = response.headers.get("content-type", "unknown")
            raise LLMError(
                f"GitHub Models returned non-JSON ({content_type}, {len(response.content)} bytes)"
            ) from exc
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("GitHub Models returned no choices")
        return _parse_json(choices[0].get("message", {}).get("content", ""))


class LLMRouter:
    """Tries each client in order and enforces a per-run call budget."""

    def __init__(self, clients: list[LLMClient], *, max_calls: int = 30):
        if not clients:
            raise ValueError("LLMRouter needs at least one client")
        self.clients = clients
        self.max_calls = max_calls
        self.calls = 0
        self.last_client_name = ""
        self.failures: list[str] = []

    @property
    def names(self) -> list[str]:
        return [client.name for client in self.clients]

    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        document: str = "",
        max_output_tokens: int = 8192,
    ) -> dict[str, Any]:
        errors: list[str] = []
        for client in self.clients:
            if self.calls >= self.max_calls:
                raise LLMError(f"AI call budget of {self.max_calls} reached for this run")
            self.calls += 1
            full_prompt = prompt
            if document:
                full_prompt += (
                    "\n\n<source_document>\n"
                    + document[: client.max_document_chars]
                    + "\n</source_document>"
                )
            try:
                result = client.generate_json(system=system, prompt=full_prompt, max_output_tokens=max_output_tokens)
            except Exception as exc:  # any provider failure means "try the next one", never a crash
                message = str(exc) if isinstance(exc, LLMError) else f"{client.name}: {type(exc).__name__}"
                errors.append(message)
                LOGGER.warning("AI provider %s failed: %s", client.name, message)
                continue
            self.last_client_name = client.name
            return result
        self.failures.extend(errors)
        raise LLMError(" | ".join(errors) or "no AI provider available")


def diagnose() -> list[dict[str, Any]]:
    """Tiny request to every configured provider/model, reporting status,
    latency and the start of the body. Never prints credentials."""
    results: list[dict[str, Any]] = []
    tiny = 'Return exactly this JSON object: {"ok": true}'

    def probe(label: str, method: str, url: str, headers: dict[str, str], **kwargs: Any) -> None:
        started = time.monotonic()
        try:
            response = httpx.request(method, url, headers=headers, timeout=90, **kwargs)
            results.append(
                {
                    "probe": label,
                    "status": response.status_code,
                    "seconds": round(time.monotonic() - started, 1),
                    "content_type": response.headers.get("content-type", ""),
                    "body_start": response.text[:300],
                }
            )
        except Exception as exc:
            results.append({"probe": label, "error": type(exc).__name__, "seconds": round(time.monotonic() - started, 1)})

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        client = GeminiClient(gemini_key)
        try:
            models = client.candidate_models()
            results.append({"probe": "gemini models", "candidates": models})
        except Exception as exc:
            models = []
            results.append({"probe": "gemini models", "error": str(exc)[:300]})
        for model in models:
            probe(
                f"gemini {model}",
                "POST",
                f"{GeminiClient.base_url}/models/{model}:generateContent",
                client._headers,
                json={
                    "contents": [{"role": "user", "parts": [{"text": tiny}]}],
                    "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 200},
                },
            )
    else:
        results.append({"probe": "gemini", "error": "GEMINI_API_KEY not set"})

    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        github = GitHubModelsClient(token)
        probe("github catalog", "GET", f"{GitHubModelsClient.base_url}/catalog/models", github._headers)
        probe(
            "github chat openai/gpt-4.1-mini",
            "POST",
            f"{GitHubModelsClient.base_url}/inference/chat/completions",
            github._headers,
            json={"model": "openai/gpt-4.1-mini", "messages": [{"role": "user", "content": tiny}], "max_tokens": 50},
        )
    else:
        results.append({"probe": "github models", "error": "GITHUB_TOKEN not set"})
    return results


def build_router(config: dict[str, Any]) -> LLMRouter | None:
    """Builds the router from environment secrets. Returns None when no
    provider is configured, so the pipeline keeps its rule-based mode."""
    llm_config = config.get("llm", {}) or {}
    if not llm_config.get("enabled", True):
        return None
    clients: list[LLMClient] = []
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        clients.append(
            GeminiClient(
                gemini_key,
                preferred_model=os.getenv("GEMINI_MODEL", str(llm_config.get("gemini_model", ""))),
                min_interval_seconds=float(llm_config.get("min_seconds_between_calls", 6)),
            )
        )
    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    if github_token and os.getenv("GITHUB_ACTIONS") == "true" and llm_config.get("github_models_fallback", True):
        clients.append(GitHubModelsClient(github_token))
    if not clients:
        return None
    return LLMRouter(clients, max_calls=int(llm_config.get("max_calls_per_run", 30)))
