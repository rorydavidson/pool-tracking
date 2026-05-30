"""Application configuration, loaded from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    app_secret: str = "dev-insecure-secret-change-me"
    base_url: str = "http://localhost:8000"
    data_dir: Path = Path("./data")

    # Claude (Anthropic) — used to generate water-chemistry advice on the fly.
    # If unset, the app falls back to a basic deterministic range check.
    anthropic_api_key: str = ""
    advice_model: str = "claude-opus-4-8"
    advice_effort: str = "medium"  # low | medium | high | max

    # Email / magic link
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = "Pool Tracking <no-reply@pooltracking.local>"
    magic_link_ttl_minutes: int = 15
    session_ttl_days: int = 30

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pool_tracking.sqlite3"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def email_enabled(self) -> bool:
        """True when a real SMTP server is configured."""
        return bool(self.smtp_host)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
