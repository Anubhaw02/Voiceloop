from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_tests_from_cloud_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent local env files from routing tests to the real Neon database."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
