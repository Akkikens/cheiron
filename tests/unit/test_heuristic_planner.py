"""The deterministic planner, asserted as whole plan objects (SPEC A5).

The golden table is the reference T09's few-shot examples are anchored to, so each row is a
plan worth imitating: filters drawn only from structured fields, no `viz_hint`, and prose that
describes what was counted without stating a count.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.engine.dimensions import REGISTRY
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan, Bin, GroupBy, Intent, Metric, StudyFilter
from app.models.request import AnalyzeRequest
from app.planner.heuristic import TEMPLATES, HeuristicPlanner, match
from app.planner.validate import validate_plan
from tests.conftest import Handler, stub_transport


@pytest.fixture
async def vocab(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


def plan_of(
    intent: Intent,
    dimension: str,
    interpretation: str,
    *,
    bin_: Bin | None = None,
    **filter_fields: Any,
) -> AnalysisPlan:
    return AnalysisPlan(
        intent=intent,
        filters=StudyFilter(**filter_fields),
        series=[],
        group_by=GroupBy(dimension=dimension, bin=bin_),
        secondary_group_by=None,
        metric=Metric.STUDY_COUNT,
        viz_hint=None,
        interpretation=interpretation,
    )


# (question, request kwargs, expected plan) — asserted whole, not field by field.
GOLDEN: list[tuple[str, dict[str, Any], AnalysisPlan]] = [
    (
        "How many trials by phase?",
        {},
        plan_of(
            Intent.DISTRIBUTION,
            "phase",
            "Distribution of clinical trials across trial phases.",
        ),
    ),
    (
        "Break down pembrolizumab trials by phase",
        {"drug_name": "Pembrolizumab"},
        plan_of(
            Intent.DISTRIBUTION,
            "phase",
            "Distribution of clinical trials studying Pembrolizumab across trial phases.",
            intervention="Pembrolizumab",
        ),
    ),
    (
        "What phases are melanoma trials in?",
        {"condition": "Melanoma"},
        plan_of(
            Intent.DISTRIBUTION,
            "phase",
            "Distribution of clinical trials in Melanoma across trial phases.",
            condition="Melanoma",
        ),
    ),
    (
        "What is the recruitment status of these trials?",
        {"condition": "Melanoma"},
        plan_of(
            Intent.DISTRIBUTION,
            "overall_status",
            "Distribution of clinical trials in Melanoma across recruitment statuses.",
            condition="Melanoma",
        ),
    ),
    (
        "Which trials are still recruiting?",
        {},
        plan_of(
            Intent.DISTRIBUTION,
            "overall_status",
            "Distribution of clinical trials across recruitment statuses.",
        ),
    ),
    (
        "How has trial activity changed over time?",
        {},
        plan_of(
            Intent.TREND,
            "start_year",
            "Annual count of clinical trials by start year.",
            bin_=Bin(size=1),
        ),
    ),
    (
        "Pembrolizumab trials by year",
        {"drug_name": "Pembrolizumab", "start_year": 2015, "end_year": 2025},
        plan_of(
            Intent.TREND,
            "start_year",
            "Annual count of clinical trials studying Pembrolizumab starting 2015-2025 by "
            "start year.",
            bin_=Bin(size=1),
            intervention="Pembrolizumab",
            start_year=2015,
            end_year=2025,
        ),
    ),
    (
        "What is the growth in interventional trials since 2020?",
        {"study_type": "INTERVENTIONAL", "start_year": 2020},
        plan_of(
            Intent.TREND,
            "start_year",
            "Annual count of interventional clinical trials starting 2020 or later by start year.",
            bin_=Bin(size=1),
            study_type="INTERVENTIONAL",
            start_year=2020,
        ),
    ),
    (
        "Which countries run the most trials?",
        {},
        plan_of(
            Intent.GEO,
            "country",
            "Geographic distribution of clinical trials by country.",
        ),
    ),
    (
        "Where are diabetes trials being run?",
        {"condition": "Diabetes"},
        plan_of(
            Intent.GEO,
            "country",
            "Geographic distribution of clinical trials in Diabetes by country.",
            condition="Diabetes",
        ),
    ),
    (
        "Who sponsors the most trials?",
        {},
        plan_of(
            Intent.DISTRIBUTION,
            "lead_sponsor",
            "Distribution of clinical trials by lead sponsor.",
        ),
    ),
    (
        "Which company is running the most phase 3 oncology trials?",
        {"condition": "Oncology", "phase": ["PHASE3"]},
        # 'phase' precedes 'company' in the table, so this is a phase distribution.
        plan_of(
            Intent.DISTRIBUTION,
            "phase",
            "Distribution of clinical trials in Oncology in Phase 3 across trial phases.",
            condition="Oncology",
            phase=["PHASE3"],
        ),
    ),
    (
        "Which funder is most active in recruiting studies?",
        {"status": ["RECRUITING"]},
        # 'recruiting' precedes 'funder', so this is a status distribution.
        plan_of(
            Intent.DISTRIBUTION,
            "overall_status",
            "Distribution of clinical trials with status Recruiting across recruitment statuses.",
            status=["RECRUITING"],
        ),
    ),
    (
        "Show me the geographic spread of Merck trials",
        {"sponsor": "Merck Sharp & Dohme LLC"},
        plan_of(
            Intent.GEO,
            "country",
            "Geographic distribution of clinical trials led by Merck Sharp & Dohme LLC by country.",
            sponsor="Merck Sharp & Dohme LLC",
        ),
    ),
    (
        "Trials in France by phase for this drug",
        {"drug_name": "Nivolumab", "country": "France"},
        plan_of(
            Intent.DISTRIBUTION,
            "phase",
            "Distribution of clinical trials studying Nivolumab with a location in France "
            "across trial phases.",
            intervention="Nivolumab",
            country="France",
        ),
    ),
]


@pytest.mark.parametrize(("question", "fields", "expected"), GOLDEN, ids=[row[0] for row in GOLDEN])
async def test_golden_plans(
    question: str, fields: dict[str, Any], expected: AnalysisPlan, vocab: Vocabulary
) -> None:
    result = await HeuristicPlanner().plan(AnalyzeRequest(query=question, **fields), vocab)

    assert result.plan == expected
    assert result.planner == "heuristic_fallback"
    assert result.attempts == 1


@pytest.mark.parametrize(("question", "fields", "expected"), GOLDEN, ids=[row[0] for row in GOLDEN])
async def test_every_golden_plan_is_valid(
    question: str, fields: dict[str, Any], expected: AnalysisPlan, vocab: Vocabulary
) -> None:
    """A reference implementation that emits plans its own validator rejects is not a reference."""
    assert validate_plan(expected, vocab) == []


async def test_golden_table_covers_every_template() -> None:
    matched = [match(question) for question, _, _ in GOLDEN]
    covered = {template.key for template in matched if template is not None}

    assert covered == {template.key for template in TEMPLATES}


async def test_planning_is_deterministic(vocab: Vocabulary) -> None:
    request = AnalyzeRequest(query="Pembrolizumab trials by phase", drug_name="Pembrolizumab")
    planner = HeuristicPlanner()

    first = await planner.plan(request, vocab)
    second = await planner.plan(request, vocab)

    assert first == second


async def test_unplannable_question_lists_what_it_can_answer(vocab: Vocabulary) -> None:
    request = AnalyzeRequest(query="What is the airspeed velocity of an unladen swallow?")

    with pytest.raises(CheironError) as caught:
        await HeuristicPlanner().plan(request, vocab)

    error = caught.value
    assert error.code is ErrorCode.UNPLANNABLE_QUERY
    assert error.status == 422
    assert len(error.details) == len(TEMPLATES)
    assert all(detail["suggestion"] for detail in error.details)


async def test_suggestions_are_questions_this_planner_actually_answers(vocab: Vocabulary) -> None:
    """A suggestion the caller retries with must not itself be unplannable."""
    planner = HeuristicPlanner()
    with pytest.raises(CheironError) as caught:
        await planner.plan(AnalyzeRequest(query="Tell me a joke about trials"), vocab)

    for detail in caught.value.details:
        retried = await planner.plan(AnalyzeRequest(query=str(detail["suggestion"])), vocab)
        assert retried.plan.group_by.dimension == detail["groups_by"]


def test_template_precedence_is_the_documented_order() -> None:
    assert [template.key for template in TEMPLATES] == [
        "phase",
        "status",
        "year",
        "country",
        "sponsor",
    ]


def test_every_template_names_a_registered_dimension() -> None:
    for template in TEMPLATES:
        assert template.dimension in REGISTRY


async def test_free_text_is_never_mined_for_filters(vocab: Vocabulary) -> None:
    """Guessing a drug name from prose would produce a confident wrong chart."""
    result = await HeuristicPlanner().plan(
        AnalyzeRequest(query="How many pembrolizumab trials by phase?"), vocab
    )

    assert result.plan.filters.intervention is None
    assert result.plan.filters.term is None


async def test_no_viz_hint_is_ever_set(vocab: Vocabulary) -> None:
    for question, fields, _ in GOLDEN:
        result = await HeuristicPlanner().plan(AnalyzeRequest(query=question, **fields), vocab)
        assert result.plan.viz_hint is None


async def test_interpretation_never_states_a_count(vocab: Vocabulary) -> None:
    for question, fields, _ in GOLDEN:
        result = await HeuristicPlanner().plan(AnalyzeRequest(query=question, **fields), vocab)
        assert "interpretation" not in validate_plan(result.plan, vocab)


async def test_the_five_templates_plan_with_no_api_key_present(
    settings: Settings, vocab: Vocabulary, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T05's done-when, and half of SPEC A6.

    The `settings` fixture is already keyless, so this asserts the precondition rather than
    assuming it — a planner that quietly needed a key would still pass every test above.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert settings.openai_api_key is None
    assert settings.llm_enabled is False

    planner = HeuristicPlanner()
    for template in TEMPLATES:
        result = await planner.plan(AnalyzeRequest(query=template.example), vocab)

        assert result.planner == "heuristic_fallback"
        assert result.plan.group_by.dimension == template.dimension
        assert validate_plan(result.plan, vocab) == []
