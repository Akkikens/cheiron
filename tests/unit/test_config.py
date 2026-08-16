from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def test_degraded_mode_needs_no_api_key() -> None:
    """SPEC §5.5 / A6: LLM_ENABLED=false must work with no key present at all."""
    settings = Settings(_env_file=None, llm_enabled=False)

    assert settings.openai_api_key is None
    assert settings.llm_enabled is False


def test_enabled_llm_without_a_key_is_a_startup_error() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY is required"):
        Settings(_env_file=None, llm_enabled=True)


def test_defaults_match_the_build_plan() -> None:
    settings = Settings(_env_file=None, llm_enabled=False)

    assert settings.ctg_base_url == "https://clinicaltrials.gov/api/v2"
    assert settings.request_budget_ms == 10_000
    assert settings.max_upstream_requests == 40
    assert settings.max_concurrency == 8
    assert settings.record_mode_threshold == 2_000


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("MAX_CONCURRENCY", "2")

    settings = Settings(_env_file=None)

    assert settings.llm_enabled is False
    assert settings.max_concurrency == 2


def test_get_settings_is_memoised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ENABLED", "false")
    get_settings.cache_clear()

    assert get_settings() is get_settings()

    get_settings.cache_clear()
