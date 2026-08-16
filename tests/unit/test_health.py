from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import REQUEST_ID_HEADER
from app.main import create_app


def test_health_reports_degraded_mode(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "llm_enabled": False}


def test_health_reports_llm_enabled() -> None:
    enabled = Settings(_env_file=None, llm_enabled=True, openai_api_key="sk-test")

    with TestClient(create_app(enabled)) as client:
        response = client.get("/health")

    assert response.json() == {"status": "ok", "llm_enabled": True}


def test_every_response_carries_a_request_id(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert len(response.headers[REQUEST_ID_HEADER]) == 32


def test_analyze_is_not_wired_yet(settings: Settings) -> None:
    """T01 ships /health only; this fails loudly the moment T07 lands without updating it."""
    with TestClient(create_app(settings)) as client:
        response = client.post("/analyze", json={"query": "trials by phase"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "invalid_request"
