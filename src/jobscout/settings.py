from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOBSCOUT_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://jobscout:jobscout@localhost:5432/jobscout"
    redis_url: str = "redis://localhost:6379/0"

    user_agent: str = "JobScout/0.1 (+https://github.com/AlidarxD/job-scout)"
    http_timeout_s: float = 20.0
    http_max_concurrency: int = 8

    # Relative to the working directory (repo root locally, /app in the container).
    sources_file: Path = Path("config/sources.yaml")

    hh_token: SecretStr | None = None
    apify_token: SecretStr | None = None
    apify_linkedin_actor: str = "worldunboxer~rapid-linkedin-scraper"

    telegram_bot_token: SecretStr | None = None  # match notifications (planned)

    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-haiku-5-5"

    storage: str = "postgres"  # postgres | memory

    # Postings not seen by any crawl for this long are marked closed.
    stale_after_days: int = 21

    @field_validator(
        "hh_token", "apify_token", "telegram_bot_token", "anthropic_api_key", mode="before"
    )
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
