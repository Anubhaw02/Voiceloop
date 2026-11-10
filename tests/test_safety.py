from pathlib import Path

import pytest

from brand_agent.config import Settings
from brand_agent.models import ContentDraft
from brand_agent.publishing import PublishingDisabledError, assert_publish_allowed


def test_publishing_is_blocked_even_for_approved_draft(tmp_path: Path):
    settings = Settings(
        raw={
            "app": {"database": "data/test.db", "export_directory": "exports"},
            "safety": {
                "publish_enabled": False,
                "require_approval": True,
                "allow_external_writes": False,
            },
        },
        profile={},
        project_root=tmp_path,
    )
    draft = ContentDraft(
        id="draft_test",
        brief_id="brief_test",
        platform="linkedin",
        title="Test",
        body="Exact approved text",
        rationale="Test",
        status="approved",
    )
    with pytest.raises(PublishingDisabledError):
        assert_publish_allowed(settings, draft)

