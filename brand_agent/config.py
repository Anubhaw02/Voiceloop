from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT / ".env.local", override=False)


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
    def database_url(self) -> str:
        configured = os.getenv("DATABASE_URL", "").strip()
        if configured:
            if configured.startswith("postgres://"):
                configured = "postgresql://" + configured[len("postgres://"):]
            if configured.startswith("postgresql://"):
                configured = "postgresql+psycopg://" + configured[len("postgresql://"):]
            return configured
        return f"sqlite+pysqlite:///{self.database_path.as_posix()}"

    @property
    def export_directory(self) -> Path:
        value = Path(self.raw["app"]["export_directory"])
        return value if value.is_absolute() else self.project_root / value

    @property
    def publish_enabled(self) -> bool:
        return _env_bool(
            "PUBLISH_ENABLED",
            bool(self.raw["safety"].get("publish_enabled", False)),
        )

    @property
    def require_approval(self) -> bool:
        return bool(self.raw["safety"].get("require_approval", True))

    @property
    def allow_external_writes(self) -> bool:
        return _env_bool(
            "ALLOW_EXTERNAL_WRITES",
            bool(self.raw["safety"].get("allow_external_writes", False)),
        )

    @property
    def environment(self) -> str:
        return os.getenv("APP_ENV", "development").strip().lower()

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def base_url(self) -> str:
        configured = os.getenv("BASE_URL", "").strip()
        if configured:
            return configured.rstrip("/")
        render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if render_host:
            return f"https://{render_host}"
        return "http://127.0.0.1:8765"

    @property
    def auth_enabled(self) -> bool:
        return _env_bool("AUTH_ENABLED", self.is_production)

    @property
    def linkedin_configured(self) -> bool:
        required = (
            "LINKEDIN_CLIENT_ID",
            "LINKEDIN_CLIENT_SECRET",
            "ADMIN_EMAIL",
            "TOKEN_ENCRYPTION_KEY",
        )
        return all(os.getenv(name, "").strip() for name in required)

    @property
    def session_secret(self) -> str:
        return os.getenv("SESSION_SECRET", "development-only-change-me")

    @property
    def allowed_hosts(self) -> list[str]:
        value = os.getenv(
            "ALLOWED_HOSTS",
            "127.0.0.1,localhost,testserver,*.onrender.com",
        )
        return [part.strip() for part in value.split(",") if part.strip()]

    def validate_runtime(self) -> None:
        if not self.is_production:
            return
        missing: list[str] = []
        for name in ("DATABASE_URL", "SESSION_SECRET", "ADMIN_EMAIL"):
            if not os.getenv(name, "").strip():
                missing.append(name)
        if not (os.getenv("BASE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()):
            missing.append("BASE_URL or RENDER_EXTERNAL_HOSTNAME")
        if self.database_url.startswith("sqlite"):
            raise ValueError("Production requires a persistent PostgreSQL DATABASE_URL")
        if self.publish_enabled and not self.allow_external_writes:
            raise ValueError("PUBLISH_ENABLED requires ALLOW_EXTERNAL_WRITES=true")
        if missing:
            raise ValueError(
                "Missing required production environment variables: "
                + ", ".join(sorted(set(missing)))
            )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    if settings.database_url.startswith("sqlite"):
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.export_directory.mkdir(parents=True, exist_ok=True)
    settings.validate_runtime()
    return settings

