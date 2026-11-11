"""Free-tier LLM access (Gemini API) with model fallback.

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
    # Some models wrap the requested object in a one-element list.
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise LLMError("model returned JSON that is not an object")
    return data


def _version_key(name: str) -> tuple[float, int]:
    match = re.search(r"(\d+(?:\.\d+)?)", name)
    version = float(match.group(1)) if match else 0.0
    stable = 0 if re.search(r"preview|exp", name) else 1
    return (version, stable)


class GeminiClient:
    """Gemini API with a Google AI Studio key (free tier).

    Model names change often, so the client asks the API which Flash models
    this key can use. Free-tier capacity varies per model: when a model is
    overloaded (503) or hangs, it is skipped for the rest of the run and the
    next model is tried. Rate limits (429) get one wait-and-retry.
    """

    base_url = "https://generativelanguage.googleapis.com/v1beta"
    max_document_chars = 120_000
    _excluded = ("image", "tts", "audio", "live", "embedding", "vision", "learnlm", "robotics", "computer-use")

    def __init__(self, api_key: str, *, preferred_model: str = "", min_interval_seconds: float = 6.0, timeout: float = 75):
        self._headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        self.preferred_model = preferred_model.strip().removeprefix("models/")
        self.min_interval_seconds = min_interval_seconds
        self.timeout = timeout
        self._models: list[str] | None = None
        self._unavailable: set[str] = set()
        self._second_chance_used = False
        self.second_chance_wait_seconds = 45.0
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
        if response.status_code >= 400:
            raise LLMError(f"Gemini model list failed (HTTP {response.status_code})")
        names = [
            model["name"].removeprefix("models/")
            for model in response.json().get("models", [])
            if "generateContent" in model.get("supportedGenerationMethods", [])
        ]
        flash = [n for n in names if "flash" in n and not any(word in n for word in self._excluded)]
        # One entry per version: prefer the stable name over previews of the same version.
        best_per_version: dict[tuple[float, bool], str] = {}
        for name in sorted(flash, key=_version_key, reverse=True):
            key = (_version_key(name)[0], "lite" in name)
            best_per_version.setdefault(key, name)
        full = sorted((n for (v, lite), n in best_per_version.items() if not lite), key=_version_key, reverse=True)
        lite = sorted((n for (v, lite), n in best_per_version.items() if lite), key=_version_key, reverse=True)
        # Newest full Flash models first, then lite models (often less congested).
        ordered = full[:4] + lite[:2]
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
        models = self.candidate_models()
        if all(model in self._unavailable for model in models) and not self._second_chance_used:
            # Overload spikes are short: once per run, wait and give every model another chance.
            self._second_chance_used = True
            LOGGER.warning("All Gemini models overloaded; waiting %.0fs before one more pass", self.second_chance_wait_seconds)
            time.sleep(self.second_chance_wait_seconds)
            self._unavailable.clear()
        for model in models:
            if model in self._unavailable:
                continue
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
                    self._unavailable.add(model)
                    break
                if response.status_code == 429 and attempt == 0:
                    try:
                        delay = min(60.0, float(response.headers.get("retry-after", "20") or 20))
                    except ValueError:
                        delay = 20.0
                    LOGGER.warning("Gemini %s rate-limited; retrying in %.0fs", model, delay)
                    time.sleep(delay)
                    continue
                if response.status_code in (429, 500, 503, 504):
                    LOGGER.warning("Gemini %s unavailable (HTTP %s); skipping it for this run", model, response.status_code)
                    failures.append(f"{model}: HTTP {response.status_code}")
                    self._unavailable.add(model)
                    break
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
                try:
                    result = _parse_json(text)
                except LLMError as exc:
                    failures.append(f"{model}: {exc}")
                    break
                self.model_used = model
                return result
        raise LLMError("Gemini failed: " + ("; ".join(failures) or "all models unavailable this run"))


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
    """Tiny request to every candidate Gemini model, reporting status,
    latency and the start of the body. Never prints credentials."""
    results: list[dict[str, Any]] = []
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return [{"probe": "gemini", "error": "GEMINI_API_KEY not set"}]
    client = GeminiClient(key)
    try:
        models = client.candidate_models()
        results.append({"probe": "gemini models", "candidates": models})
    except Exception as exc:
        return [{"probe": "gemini models", "error": str(exc)[:300]}]
    for model in models:
        started = time.monotonic()
        try:
            response = httpx.post(
                f"{GeminiClient.base_url}/models/{model}:generateContent",
                headers=client._headers,
                json={
                    "contents": [{"role": "user", "parts": [{"text": 'Return exactly this JSON object: {"ok": true}'}]}],
                    "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 200},
                },
                timeout=90,
            )
            results.append(
                {
                    "probe": model,
                    "status": response.status_code,
                    "seconds": round(time.monotonic() - started, 1),
                    "body_start": response.text[:200],
                }
            )
        except Exception as exc:
            results.append({"probe": model, "error": type(exc).__name__, "seconds": round(time.monotonic() - started, 1)})
    return results


def build_router(config: dict[str, Any]) -> LLMRouter | None:
    """Builds the router from environment secrets. Returns None when no
    provider is configured, so the pipeline keeps its rule-based mode."""
    llm_config = config.get("llm", {}) or {}
    if not llm_config.get("enabled", True):
        return None
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        return None
    client = GeminiClient(
        gemini_key,
        preferred_model=os.getenv("GEMINI_MODEL", str(llm_config.get("gemini_model", ""))),
        min_interval_seconds=float(llm_config.get("min_seconds_between_calls", 6)),
    )
    return LLMRouter([client], max_calls=int(llm_config.get("max_calls_per_run", 30)))
