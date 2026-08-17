"""A7: safety rules. SPEC §8.

Both rules are non-overridable, and both are driven here through the **LLM path** with a scripted
completer, because the heuristic planner cannot express either case: it never emits
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
from tests.unit.test_records import PagingUpstream, phase_fixture


@pytest.fixture
def llm_settings() -> Settings:
    return Settings(_env_file=None, llm_enabled=True, openai_api_key="sk-test-not-used")


def scripted(payload: dict[str, Any]) -> Any:
    async def completer(messages: Sequence[dict[str, str]], schema: dict[str, Any]) -> str:
        return json.dumps(payload)

    return completer


def ask(settings: Settings, payload: dict[str, Any], handler: Any = None) -> Any:
    app = create_app(
        settings,
        transport=stub_transport(settings, handler or a1_upstream().handler()),
        completer=scripted(payload),
    )
    with TestClient(app) as client:
        return client.post("/analyze", json={"query": "anything", "drug_name": "Pembrolizumab"})


def under_the_threshold(*, statuses: tuple[str, ...] = ()) -> Any:
    """600 studies: small enough for `complete_records`, where both rules stop applying.

    `statuses` spreads a real partition across the result set, so a dimension that is genuinely
    one-value-per-study still produces more than one bucket.
    """
    studies = phase_fixture(600)
    for index, study in enumerate(studies):
        if statuses:
            study["protocolSection"]["statusModule"]["overallStatus"] = statuses[
                index % len(statuses)
            ]
    return PagingUpstream(studies, total=600).handler()


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


def test_a7_network_below_the_threshold_is_not_downgraded(llm_settings: Settings) -> None:
    """The downgrade is conditional, and a rule that always fires is not a safety rule.

    In `complete_records` mode the co-occurrence is computed from the full result set, so it is
    exact and unbiased and there is nothing to protect the caller from.
    """
    payload = a_plan_payload(intent="network")
    payload["group_by"] = {"dimension": "intervention_name", "bin": None}
    response = ask(llm_settings, payload, under_the_threshold())
    assert_contract(response)

    body = response.json()
    assert body["visualization"]["type"] == "network_graph"
    assert set(body["visualization"]["data"]) == {"nodes", "edges"}
    assert not any("downgrad" in warning for warning in body["meta"]["warnings"])


def test_a7_a_pie_hint_is_refused_on_a_partition_dimension_too(llm_settings: Settings) -> None:
    """The pie rule is not dimension-dependent, and this is what pins that reading of A7.

    SPEC A7 names the non-partition case because that is where a pie is actively false: segments
    that overlap cannot be shares of a whole. But pie and donut are not renderable types at all
    (T04 drops them at parse), so `overall_status`, a real partition where every study has exactly
    one value, is refused for the same reason. Without this test A7 reads as though a pie would be
    drawn given a well-behaved dimension.
    """
    payload = a_plan_payload(viz_hint="pie_chart")
    payload["group_by"] = {"dimension": "overall_status", "bin": None}
    response = ask(
        llm_settings,
        payload,
        under_the_threshold(statuses=("RECRUITING", "COMPLETED", "TERMINATED")),
    )
    assert_contract(response)

    body = response.json()
    assert body["visualization"]["type"] == "bar_chart"
    warning = next(w for w in body["meta"]["warnings"] if "pie_chart" in w)
    assert "not a renderable chart type" in warning


def test_a7_a_renderable_hint_that_would_imply_a_false_whole_is_discarded(
    llm_settings: Settings,
) -> None:
    """`stacked_bar_chart` is a real chart type, and that is what makes this the harder case.

    Stacking phase, which is multi-valued, gives segments that sum to more than the bar they sit
    in. The rule is on the dimension supplying the segments, not on the chart type.
    """
    payload = a_plan_payload(viz_hint="stacked_bar_chart")
    response = ask(llm_settings, payload, under_the_threshold())
    assert_contract(response)

    body = response.json()
    assert body["visualization"]["type"] != "stacked_bar_chart"
    warning = next(w for w in body["meta"]["warnings"] if "stacked_bar_chart" in w)
    assert "safety rule" in warning
