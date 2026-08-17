from __future__ import annotations

from functools import lru_cache
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openai_api_key: str | None = None
    llm_enabled: bool = True

    ctg_base_url: str = "https://clinicaltrials.gov/api/v2"

    request_budget_ms: int = Field(default=10_000, gt=0)
    max_upstream_requests: int = Field(default=40, gt=0)
    max_concurrency: int = Field(default=8, gt=0)
    record_mode_threshold: int = Field(default=2_000, gt=0)
    sample_pages: int = Field(default=3, gt=0)
    """Label-discovery pages in `sampled_then_confirmed`. Pages are serial, so this is a direct
    latency cost; 3 pages of 1,000 studies is the most that fits the request budget alongside the
    confirmation wave."""

    @model_validator(mode="after")
    def _require_key_only_when_enabled(self) -> Self:
        if self.llm_enabled and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when LLM_ENABLED=true")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
