from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import REQUEST_ID_HEADER
from app.main import create_app
from tests.conftest import Handler, stub_transport


def _client(settings: Settings, handler: Handler) -> TestClient:
    return TestClient(create_app(settings, transport=stub_transport(settings, handler)))


def test_health_reports_degraded_mode(settings: Settings, enums_handler: Handler) -> None:
    with _client(settings, enums_handler) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "llm_enabled": False, "vocabulary": "ok"}


def test_health_reports_llm_enabled(enums_handler: Handler) -> None:
    enabled = Settings(_env_file=None, llm_enabled=True, openai_api_key="sk-test")

    with _client(enabled, enums_handler) as client:
        response = client.get("/health")

    assert response.json()["llm_enabled"] is True


def test_unreachable_enums_does_not_stop_startup(settings: Settings) -> None:
    """T02: a cold /studies/enums degrades /health, it does not take the process down."""

    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="Service Unavailable")

    with _client(settings, failing) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["vocabulary"] == "unavailable"


def test_every_response_carries_a_request_id(settings: Settings, enums_handler: Handler) -> None:
    with _client(settings, enums_handler) as client:
        response = client.get("/health")

    assert len(response.headers[REQUEST_ID_HEADER]) == 32


def test_analyze_is_not_wired_yet(settings: Settings, enums_handler: Handler) -> None:
    """T02 ships /health only; this fails loudly the moment T07 lands without updating it."""
    with _client(settings, enums_handler) as client:
        response = client.post("/analyze", json={"query": "trials by phase"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invalid_request"
