"""A5: planner determinism. SPEC §8.

Asserts the **IR**, not prose: a fixed question yields a fixed `AnalysisPlan`. Run at the planner
level rather than through HTTP on purpose: the plan is the contract being pinned here, and
routing twenty questions through the engine would test the engine's stubs instead.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.models.request import AnalyzeRequest
from app.planner.heuristic import HeuristicPlanner
from tests.conftest import Handler, stub_transport

GOLDEN: list[tuple[dict[str, object], str, str]] = [
    ({"query": "How many trials by phase?"}, "distribution", "phase"),
    ({"query": "Break these down by trial phase"}, "distribution", "phase"),
    ({"query": "phase distribution please"}, "distribution", "phase"),
    ({"query": "What is the recruitment status?"}, "distribution", "overall_status"),
    ({"query": "How many are still recruiting?"}, "distribution", "overall_status"),
    ({"query": "Show me completed studies"}, "distribution", "overall_status"),
    ({"query": "Which ones are active?"}, "distribution", "overall_status"),
    ({"query": "How has this changed over time?"}, "trend", "start_year"),
    ({"query": "Show the trend since 2015"}, "trend", "start_year"),
    ({"query": "Trials by year"}, "trend", "start_year"),
    ({"query": "What has the growth been?"}, "trend", "start_year"),
    ({"query": "Where are these trials run?"}, "geo", "country"),
    ({"query": "Which countries have the most?"}, "geo", "country"),
    ({"query": "Geographic distribution of studies"}, "geo", "country"),
    ({"query": "Which region runs the most?"}, "geo", "country"),
    ({"query": "Who sponsors the most trials?"}, "distribution", "lead_sponsor"),
    ({"query": "Which company runs these?"}, "distribution", "lead_sponsor"),
    ({"query": "Who is running these studies?"}, "distribution", "lead_sponsor"),
    ({"query": "Top funders?"}, "distribution", "lead_sponsor"),
    (
        {"query": "How many trials by phase?", "drug_name": "Pembrolizumab", "start_year": 2015},
        "distribution",
        "phase",
    ),
    # The six shapes added when the deterministic planner was broadened. They matter to A5
    # specifically because they sit *above* the original five in precedence, so a keyword added
    # carelessly to any of them would silently re-route a question that used to work.
    ({"query": "Which interventions are studied together?"}, "network", "intervention_name"),
    ({"query": "Which drugs are used with this one?"}, "network", "intervention_name"),
    ({"query": "How big are these trials?"}, "histogram", "enrollment_count"),
    ({"query": "What is the typical enrollment?"}, "histogram", "enrollment_count"),
    ({"query": "How many are industry funded?"}, "distribution", "sponsor_class"),
    ({"query": "Are these interventional or observational?"}, "distribution", "study_type"),
    ({"query": "What types of intervention are studied?"}, "distribution", "intervention_type"),
    ({"query": "Which conditions are studied?"}, "distribution", "condition"),
    # The collision that broadening introduced: a trend question that names a study type.
    ({"query": "What is the growth in interventional trials since 2020?"}, "trend", "start_year"),
]


@pytest.fixture
async def vocab(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


@pytest.mark.parametrize(("payload", "intent", "dimension"), GOLDEN)
async def test_a5_a_question_yields_a_stable_plan(
    payload: dict[str, object], intent: str, dimension: str, vocab: Vocabulary
) -> None:
    request = AnalyzeRequest.model_validate(payload)
    planner = HeuristicPlanner()

    first = await planner.plan(request, vocab)
    second = await planner.plan(request, vocab)

    assert first.plan.intent.value == intent
    assert first.plan.group_by.dimension == dimension
    # The whole IR, not just the two fields under test: a drift anywhere else fails here.
    assert first.plan == second.plan


async def test_a5_structured_hints_reach_the_plan(vocab: Vocabulary) -> None:
    request = AnalyzeRequest.model_validate(
        {"query": "How many trials by phase?", "drug_name": "Pembrolizumab", "start_year": 2015}
    )

    result = await HeuristicPlanner().plan(request, vocab)

    assert result.plan.filters.intervention == "Pembrolizumab"
    assert result.plan.filters.start_year == 2015


async def test_a5_interpretation_never_carries_a_count(vocab: Vocabulary) -> None:
    """Prose is generated from the plan; it must never assert a result."""
    for payload, _, _ in GOLDEN:
        result = await HeuristicPlanner().plan(AnalyzeRequest.model_validate(payload), vocab)
        digits = [token for token in result.plan.interpretation.split() if token.isdigit()]
        assert all(len(token) == 4 for token in digits)  # years only
