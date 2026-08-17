"""A7 — safety rules. SPEC §8.

Both rules are non-overridable, and both are driven here through the **LLM path** with a scripted
completer, because the heuristic planner cannot express either case — it never emits
`intent=network` and never sets `viz_hint`. That makes this also the end-to-end proof that a
model-authored plan flows through the engine and out as a chart.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.acceptance.conftest import assert_contract
from tests.conftest import stub_transport
from tests.unit.test_engine_counts import a1_upstream
from tests.unit.test_llm_planner import a_plan_payload


@pytest.fixture
def llm_settings() -> Settings:
    return Settings(_env_file=None, llm_enabled=True, openai_api_key="sk-test-not-used")


def scripted(payload: dict[str, Any]) -> Any:
    async def completer(messages: Sequence[dict[str, str]], schema: dict[str, Any]) -> str:
        return json.dumps(payload)

    return completer


def ask(settings: Settings, payload: dict[str, Any]) -> Any:
    upstream = a1_upstream()
    app = create_app(
        settings,
        transport=stub_transport(settings, upstream.handler()),
        completer=scripted(payload),
    )
    with TestClient(app) as client:
        return client.post("/analyze", json={"query": "anything", "drug_name": "Pembrolizumab"})


def test_a7_network_above_the_threshold_downgrades_and_warns(llm_settings: Settings) -> None:
    """SPEC §5.4: co-occurrence from anything but the full set would look authoritative and lie."""
    response = ask(llm_settings, a_plan_payload(intent="network"))
    assert_contract(response)

    body = response.json()
    assert body["visualization"]["type"] == "grouped_bar_chart"
    warning = next(w for w in body["meta"]["warnings"] if "network_graph" in w)
    assert "complete_records" in warning
    assert "2,927" in warning  # the observed total, so the caller knows why


def test_a7_a_pie_hint_on_an_overlapping_dimension_is_discarded(llm_settings: Settings) -> None:
    """A pie over overlapping buckets asserts a whole that does not exist."""
    response = ask(llm_settings, a_plan_payload(viz_hint="pie_chart"))
    assert_contract(response)

    body = response.json()
    assert body["visualization"]["type"] == "bar_chart"
    assert any("pie_chart" in w for w in body["meta"]["warnings"])


def test_a7_the_llm_path_produces_a_real_chart(llm_settings: Settings) -> None:
    """The model plans; deterministic code produces every number."""
    response = ask(llm_settings, a_plan_payload())
    assert_contract(response)

    body = response.json()
    assert body["meta"]["planner"] == "llm"
    assert body["meta"]["total_matching_studies"] == 2_927
    assert body["visualization"]["data"]
