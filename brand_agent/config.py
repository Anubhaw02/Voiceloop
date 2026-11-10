from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Settings:
    raw: dict[str, Any]
    profile: dict[str, Any]
    project_root: Path = PROJECT_ROOT

    @property
    def database_path(self) -> Path:
        value = Path(self.raw["app"]["database"])
        return value if value.is_absolute() else self.project_root / value

    @property
    def export_directory(self) -> Path:
        value = Path(self.raw["app"]["export_directory"])
        return value if value.is_absolute() else self.project_root / value

    @property
    def publish_enabled(self) -> bool:
        return bool(self.raw["safety"].get("publish_enabled", False))

    @property
    def require_approval(self) -> bool:
        return bool(self.raw["safety"].get("require_approval", True))


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a mapping in {path}")
    return data


def load_settings(
    agent_path: Path | None = None,
    profile_path: Path | None = None,
) -> Settings:
    agent_path = agent_path or PROJECT_ROOT / "config" / "agent.yaml"
    profile_path = profile_path or PROJECT_ROOT / "config" / "profile.yaml"
    settings = Settings(raw=_load_yaml(agent_path), profile=_load_yaml(profile_path))
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.export_directory.mkdir(parents=True, exist_ok=True)
    return settings

