"""A6 — degraded mode. SPEC §8.

With `LLM_ENABLED=false` and **no API key anywhere in the environment**, the questions the
fallback planner covers still return valid, correct specs. This is the half T05 could only prove
at the `Settings` level; here it goes through the API.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from tests.acceptance.conftest import analyze, assert_contract
from tests.unit.test_engine_counts import A1_BUCKETS, A1_TOTAL, a1_upstream

QUESTION = {"query": "How many trials by phase?", "drug_name": "Pembrolizumab"}


@pytest.fixture
def keyless(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Not merely unset in Settings — removed from the process environment entirely."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return Settings(_env_file=None, llm_enabled=False)


def test_a6_answers_correctly_with_no_key_present(keyless: Settings) -> None:
    upstream = a1_upstream()
    response = analyze(keyless, upstream.handler(), QUESTION)
    assert_contract(response)

    body = response.json()
    assert body["meta"]["planner"] == "heuristic_fallback"
    assert body["meta"]["total_matching_studies"] == A1_TOTAL
    assert {r["phase"]: r["study_count"] for r in body["visualization"]["data"]} == A1_BUCKETS


def test_a6_health_declares_the_degraded_mode(keyless: Settings) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import stub_transport

    upstream = a1_upstream()
    app = create_app(keyless, transport=stub_transport(keyless, upstream.handler()))
    with TestClient(app) as client:
        body = client.get("/health").json()

    assert body["llm_enabled"] is False


def test_a6_a_question_outside_the_templates_is_refused_not_guessed(keyless: Settings) -> None:
    """Degraded coverage is stated, not faked: an uncovered question gets a refusal."""
    upstream = a1_upstream()
    response = analyze(keyless, upstream.handler(), {"query": "What is the airspeed of a swallow?"})
    assert_contract(response)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unplannable_query"
