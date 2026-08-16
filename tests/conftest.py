from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.ctg.client import CTGTransport

FIXTURES = Path(__file__).parent / "fixtures"
UPSTREAM = FIXTURES / "upstream"


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


def load_fixture(name: str) -> Any:
    return json.loads((UPSTREAM / name).read_text())


def fixture_text(name: str) -> str:
    return (UPSTREAM / name).read_text()


Handler = Callable[[httpx.Request], httpx.Response]
AsyncHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def stub_transport(
    settings: Settings, handler: Handler | AsyncHandler, **kwargs: Any
) -> CTGTransport:
    """A `CTGTransport` wired to an in-memory handler. Tests never touch the network."""
    http = httpx.AsyncClient(
        base_url=settings.ctg_base_url,
        # MockTransport dispatches sync or async handlers; an async one lets a test force real
        # interleaving, which is what makes a concurrency assertion non-vacuous.
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    kwargs.setdefault("sleep", _no_sleep)
    return CTGTransport(settings, http=http, **kwargs)


async def _no_sleep(_seconds: float) -> None:
    """Backoff and rate-limit waits are asserted on, not slept through."""
    return None


@pytest.fixture
def enums_handler() -> Handler:
    """Serves the recorded `/studies/enums` and `/version` bodies with their real ETag."""
    enums = fixture_text("studies_enums.json")
    version = fixture_text("version.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/studies/enums"):
            return httpx.Response(200, text=enums, headers={"ETag": '"883b003/0.34.1/msspuzuw"'})
        if request.url.path.endswith("/version"):
            return httpx.Response(200, text=version, headers={"ETag": '"883b003/0.34.1/msspuzuw"'})
        return httpx.Response(404, text="not stubbed")

    return handler
