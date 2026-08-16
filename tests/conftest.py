from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Never let a developer's real `.env` decide what the tests assert."""
    for var in (
        "OPENAI_API_KEY",
        "LLM_ENABLED",
        "CTG_BASE_URL",
        "REQUEST_BUDGET_MS",
        "MAX_UPSTREAM_REQUESTS",
        "MAX_CONCURRENCY",
        "RECORD_MODE_THRESHOLD",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture
def settings() -> Settings:
    """Degraded-mode settings: no API key anywhere, per SPEC §5.5 / A6."""
    return Settings(_env_file=None, llm_enabled=False)
