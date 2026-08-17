"""`POST /analyze` end-to-end over the injectable transport seam. SPEC §4, A1, A4, A7."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import stub_transport
from tests.unit.test_engine_counts import (
    A1_BUCKETS,
    A1_MISSING,
    A1_SUM,
    A1_TOTAL,
    Upstream,
    a1_upstream,
)


def make_client(settings: Settings, upstream: Upstream) -> TestClient:
    transport = stub_transport(settings, upstream.async_handler)
    app = create_app(settings, transport=transport)
    return TestClient(app)


async def _warm(settings: Settings, upstream: Upstream) -> TestClient:
    """TestClient triggers lifespan; reset version_reads after vocab warm."""
    client = make_client(settings, upstream)
    # Entering the context runs lifespan startup (vocab warm + version).
    client.__enter__()
    upstream.version_reads = 0
    upstream.requests.clear()
    return client


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, llm_enabled=False)


@pytest.mark.asyncio
async def test_analyze_returns_a1_bar_chart(settings: Settings) -> None:
    upstream = a1_upstream()
    client = await _warm(settings, upstream)
    try:
        response = client.post(
            "/analyze",
            json={"query": "How many trials by phase?", "drug_name": "Pembrolizumab"},
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 200, response.text
    body = response.json()
    viz = body["visualization"]
    meta = body["meta"]

    assert viz["type"] == "bar_chart"
    assert viz["title"] == "Pembrolizumab Trials by Phase"
    assert "2,927 studies" in viz["subtitle"]
    assert viz["encoding"]["x"]["field"] == "phase"
    assert viz["encoding"]["x"]["sort"][-1] == "MISSING"
    assert viz["encoding"]["y"]["field"] == "study_count"

    by_key = {row["phase"]: row["study_count"] for row in viz["data"]}
    assert by_key == A1_BUCKETS

    assert meta["planner"] == "heuristic_fallback"
    assert meta["total_matching_studies"] == A1_TOTAL
    assert meta["coverage"]["bucket_sum"] == A1_SUM
    assert meta["coverage"]["unclassified_count"] == A1_MISSING
    assert meta["coverage"]["groupby_semantics"] == "overlapping"
    assert meta["coverage"]["overlap_note"] is not None
    assert "515" in meta["coverage"]["overlap_note"]
    assert meta["filters_applied"]["intervention"] == "Pembrolizumab"
    assert "api_query_log" not in meta or meta["api_query_log"] is None
    assert meta.get("plan") is None

    # Contract: response validates as AnalyzeResponse (no share under overlapping).
    serialized = response.text
    assert "share_of_total" not in serialized
    assert "truncated" not in serialized


@pytest.mark.asyncio
async def test_analyze_response_validates_against_t04_models(settings: Settings) -> None:
    from app.models.response import AnalyzeResponse

    upstream = a1_upstream()
    client = await _warm(settings, upstream)
    try:
        response = client.post(
            "/analyze",
            json={"query": "How many trials by phase?", "drug_name": "Pembrolizumab"},
        )
    finally:
        client.__exit__(None, None, None)

    AnalyzeResponse.model_validate(response.json())


@pytest.mark.asyncio
async def test_explain_includes_query_log_and_plan(settings: Settings) -> None:
    upstream = a1_upstream()
    client = await _warm(settings, upstream)
    try:
        response = client.post(
            "/analyze",
            json={
                "query": "How many trials by phase?",
                "drug_name": "Pembrolizumab",
                "options": {"explain": True},
            },
        )
    finally:
        client.__exit__(None, None, None)

    meta = response.json()["meta"]
    assert meta["api_query_log"]
    assert all("clinicaltrials.gov" in url for url in meta["api_query_log"])
    assert meta["plan"]["group_by"]["dimension"] == "phase"


@pytest.mark.asyncio
async def test_zero_results_a4(settings: Settings) -> None:
    upstream = Upstream(
        {None: 0, "AREA[Phase]MISSING": 0} | {f"AREA[Phase]{value}": 0 for value in A1_BUCKETS}
    )
    client = await _warm(settings, upstream)
    try:
        response = client.post(
            "/analyze",
            json={"query": "How many trials by phase?", "drug_name": "NoSuchDrugXYZ"},
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 200
    body = response.json()
    assert body["visualization"]["data"] == []
    assert body["meta"]["total_matching_studies"] == 0
    assert any("No studies" in w or "empty" in w.lower() for w in body["meta"]["warnings"])


@pytest.mark.asyncio
async def test_pie_hint_via_plan_path_is_not_applicable_to_heuristic() -> None:
    """Heuristic never sets viz_hint; A7 is covered at the registry with a parsed plan."""
    plan = __import__("app.models.plan", fromlist=["AnalysisPlan"]).AnalysisPlan.model_validate(
        {
            "intent": "distribution",
            "filters": {"intervention": "Pembrolizumab"},
            "group_by": {"dimension": "phase"},
            "interpretation": (
                "Distribution of clinical trials studying Pembrolizumab across phases."
            ),
            "viz_hint": "pie_chart",
        }
    )
    assert plan.discarded_viz_hint == "pie_chart"


@pytest.mark.asyncio
async def test_unplannable_query_code_not_invalid_request(settings: Settings) -> None:
    upstream = a1_upstream()
    client = await _warm(settings, upstream)
    try:
        response = client.post(
            "/analyze",
            json={"query": "What is the airspeed velocity of an unladen swallow?"},
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unplannable_query"


@pytest.mark.asyncio
async def test_invalid_enum_is_invalid_request(settings: Settings) -> None:
    upstream = a1_upstream()
    client = await _warm(settings, upstream)
    try:
        response = client.post(
            "/analyze",
            json={"query": "trials by phase", "phase": ["PHASE9"]},
        )
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
