from __future__ import annotations

import os

import pytest

# Runs before any brand_agent import. load_dotenv(override=False) never
# replaces a variable that already exists, so blank values here stop
# .env.local from pointing module-level code (brand_agent.web.app) at Neon.
for _name in ("DATABASE_URL", "GEMINI_API_KEY", "GEMINI_MODEL", "GITHUB_TOKEN", "OPENALEX_API_KEY", "RESEND_API_KEY"):
    os.environ[_name] = ""


@pytest.fixture(autouse=True)
def isolate_tests_from_cloud_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent local env files from routing tests to the real Neon database
    or to real AI / research providers."""
    for name in ("DATABASE_URL", "GEMINI_API_KEY", "GEMINI_MODEL", "GITHUB_TOKEN", "OPENALEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
