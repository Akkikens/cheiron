"""`validate_plan` and `enforce_hard_constraints`. SPEC §3, §2.1.

Every rule gets a failing case and a passing case, because a validator that only ever fires is
as broken as one that never does.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.errors import CheironError, ErrorCode
from app.models.plan import (
    AnalysisPlan,
    Bin,
    GroupBy,
    Intent,
    Metric,
    SeriesSpec,
    StudyFilter,
)
from app.models.request import AnalyzeRequest
from app.planner import validate as validate_module
from app.planner.validate import enforce_hard_constraints, validate_plan
from tests.conftest import Handler, stub_transport


@pytest.fixture
async def vocab(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


def a_plan(**overrides: Any) -> AnalysisPlan:
    base: dict[str, Any] = {
        "intent": Intent.DISTRIBUTION,
        "filters": StudyFilter(),
        "group_by": GroupBy(dimension="phase"),
        "interpretation": "Distribution of clinical trials across trial phases.",
    }
    return AnalysisPlan(**{**base, **overrides})


def only_error(plan: AnalysisPlan, vocab: Vocabulary) -> str:
    errors = validate_plan(plan, vocab)
    assert len(errors) == 1, f"expected exactly one error, got {errors}"
    return errors[0]


def test_a_good_plan_has_no_errors(vocab: Vocabulary) -> None:
    assert validate_plan(a_plan(), vocab) == []


# --- rule 1: dimensions exist ---------------------------------------------------------------


def test_unknown_group_by_dimension(vocab: Vocabulary) -> None:
    message = only_error(a_plan(group_by=GroupBy(dimension="phasez")), vocab)

    assert "group_by.dimension is 'phasez'" in message
    assert "lead_sponsor" in message


def test_unknown_secondary_group_by_dimension(vocab: Vocabulary) -> None:
    plan = a_plan(secondary_group_by=GroupBy(dimension="funding_source"))

    assert "secondary_group_by.dimension is 'funding_source'" in only_error(plan, vocab)


def test_known_secondary_group_by_passes(vocab: Vocabulary) -> None:
    assert validate_plan(a_plan(secondary_group_by=GroupBy(dimension="study_type")), vocab) == []


def test_secondary_group_by_may_not_repeat_the_primary(vocab: Vocabulary) -> None:
    plan = a_plan(secondary_group_by=GroupBy(dimension="phase"))

    assert "repeats group_by.dimension" in only_error(plan, vocab)


# --- rule 1b: bin only where binning means something ---------------------------------------


def test_bin_on_a_non_temporal_dimension(vocab: Vocabulary) -> None:
    plan = a_plan(group_by=GroupBy(dimension="phase", bin=Bin(size=5)))

    message = only_error(plan, vocab)
    assert "bin is set on 'phase'" in message
    assert "start_year" in message


def test_bin_on_a_temporal_dimension_passes(vocab: Vocabulary) -> None:
    plan = a_plan(
        intent=Intent.TREND,
        group_by=GroupBy(dimension="start_year", bin=Bin(size=1)),
        interpretation="Annual count of clinical trials by start year.",
    )

    assert validate_plan(plan, vocab) == []


# --- rule 2: enum values are live-valid ----------------------------------------------------


def test_unknown_phase_value(vocab: Vocabulary) -> None:
    plan = a_plan(filters=StudyFilter(phase=["PHASE9"]))

    message = only_error(plan, vocab)
    assert "filters.phase contains 'PHASE9'" in message
    assert "PHASE3" in message


def test_unknown_status_value(vocab: Vocabulary) -> None:
    plan = a_plan(filters=StudyFilter(status=["ENROLLING"]))

    assert "not a live Status value" in only_error(plan, vocab)


def test_unknown_study_type_value(vocab: Vocabulary) -> None:
    plan = a_plan(filters=StudyFilter(study_type="EXPERIMENTAL"))

    assert "not a live StudyType value" in only_error(plan, vocab)


def test_enum_values_in_series_are_checked_too(vocab: Vocabulary) -> None:
    plan = a_plan(
        intent=Intent.COMPARISON,
        series=[
            SeriesSpec(label="Valid", filters=StudyFilter(phase=["PHASE3"])),
            SeriesSpec(label="Invalid", filters=StudyFilter(phase=["PHASE_III"])),
        ],
    )

    assert "series[1].filters.phase contains 'PHASE_III'" in only_error(plan, vocab)


def test_valid_enum_values_pass(vocab: Vocabulary) -> None:
    plan = a_plan(
        filters=StudyFilter(
            phase=["PHASE2", "PHASE3"], status=["RECRUITING"], study_type="INTERVENTIONAL"
        ),
        interpretation="Distribution of clinical trials across trial phases.",
    )

    assert validate_plan(plan, vocab) == []


# --- rule 3: coherence ---------------------------------------------------------------------


def test_network_intent_requires_study_count(vocab: Vocabulary) -> None:
    plan = a_plan(intent=Intent.NETWORK, metric=Metric.ENROLLMENT_MEDIAN)

    messages = validate_plan(plan, vocab)
    assert any("intent is 'network' but metric is 'enrollment_median'" in m for m in messages)


def test_network_intent_with_study_count_passes(vocab: Vocabulary) -> None:
    assert validate_plan(a_plan(intent=Intent.NETWORK), vocab) == []


def test_trend_intent_requires_a_temporal_dimension(vocab: Vocabulary) -> None:
    plan = a_plan(intent=Intent.TREND, group_by=GroupBy(dimension="country"))

    message = only_error(plan, vocab)
    assert "intent is 'trend'" in message
    assert "start_year" in message


def test_geo_intent_requires_country(vocab: Vocabulary) -> None:
    plan = a_plan(intent=Intent.GEO, group_by=GroupBy(dimension="phase"))

    assert "intent is 'geo' but group_by.dimension is 'phase'" in only_error(plan, vocab)


def test_geo_intent_with_country_passes(vocab: Vocabulary) -> None:
    plan = a_plan(
        intent=Intent.GEO,
        group_by=GroupBy(dimension="country"),
        interpretation="Geographic distribution of clinical trials by country.",
    )

    assert validate_plan(plan, vocab) == []


def test_comparison_intent_requires_two_series(vocab: Vocabulary) -> None:
    plan = a_plan(intent=Intent.COMPARISON)

    assert "series has 0 entries" in only_error(plan, vocab)


def test_comparison_intent_with_two_series_passes(vocab: Vocabulary) -> None:
    plan = a_plan(
        intent=Intent.COMPARISON,
        series=[
            SeriesSpec(label="Merck", filters=StudyFilter(sponsor="Merck Sharp & Dohme LLC")),
            SeriesSpec(label="Pfizer", filters=StudyFilter(sponsor="Pfizer")),
        ],
    )

    assert validate_plan(plan, vocab) == []


def test_multiple_series_without_comparison_is_refused_at_parse_time() -> None:
    """The one coherence rule `validate_plan` does not check, because it cannot be built.

    The message still has to read as repair input, since T09 feeds parse failures back too.
    """
    with pytest.raises(ValidationError) as caught:
        a_plan(
            intent=Intent.DISTRIBUTION,
            series=[
                SeriesSpec(label="A", filters=StudyFilter(sponsor="Merck Sharp & Dohme LLC")),
                SeriesSpec(label="B", filters=StudyFilter(sponsor="Pfizer")),
            ],
        )

    assert "requires intent=comparison" in str(caught.value)


def test_scatter_does_not_require_a_secondary_dimension(vocab: Vocabulary) -> None:
    """Scatter plots enrollment against start date, so the second axis is not a plan choice.

    It used to demand `secondary_group_by`. Requiring a field the renderer ignores invites a
    caller to set it and expect it to change the chart.
    """
    plan = a_plan(intent=Intent.SCATTER, group_by=GroupBy(dimension="enrollment_count"))

    assert validate_plan(plan, vocab) == []


@pytest.mark.parametrize("intent", [Intent.SCATTER, Intent.HISTOGRAM])
def test_these_intents_are_now_plannable(intent: Intent, vocab: Vocabulary) -> None:
    """`enrollment_count` exists, so SPEC §6.1's scatter_plot and histogram rows are reachable.

    This test previously asserted the opposite. The rule was always keyed on the registry rather
    than hardcoded, so adding one dimension row lifted the refusal, which is what the
    monkeypatch test below was written to guarantee, and now demonstrates for real.
    """
    plan = a_plan(intent=intent, group_by=GroupBy(dimension="enrollment_count"))

    assert validate_plan(plan, vocab) == []


@pytest.mark.parametrize("intent", [Intent.SCATTER, Intent.HISTOGRAM])
def test_these_intents_need_the_quantitative_dimension_on_group_by(
    intent: Intent, vocab: Vocabulary
) -> None:
    """Reachable is not the same as unconditional: the dimension has to actually be the axis."""
    plan = a_plan(intent=intent, group_by=GroupBy(dimension="phase"))

    message = only_error(plan, vocab)
    assert "not quantitative" in message
    assert "enrollment_count" in message


@pytest.mark.parametrize("intent", [Intent.SCATTER, Intent.HISTOGRAM])
def test_these_intents_parse_and_validate(intent: Intent, vocab: Vocabulary) -> None:
    """`json_schema_strict()` publishes every intent as the model's action space.

    A plan carrying one must be *parseable* whatever the verdict: otherwise T09's repair loop
    sees a malformed response rather than an unservable request, and repairs the wrong thing.
    """
    plan = AnalysisPlan.model_validate(
        {
            "intent": intent.value,
            "filters": {},
            "group_by": {"dimension": "enrollment_count"},
            "interpretation": "Distribution of clinical trials across enrollment sizes.",
        }
    )

    assert plan.intent is intent
    assert intent.value in AnalysisPlan.json_schema_strict()["$defs"]["Intent"]["enum"]
    assert validate_plan(plan, vocab) == []


def test_the_refusal_returns_if_the_quantitative_dimension_goes_away(
    vocab: Vocabulary, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule reads the registry rather than hardcoding either verdict.

    Kept inverted from its original direction so the refusal path stays covered now that the
    real registry lifts it, and so the message keeps naming the real blocker rather than the
    symptom.
    """
    monkeypatch.setattr(validate_module, "QUANTITATIVE_KEYS", frozenset())
    plan = a_plan(intent=Intent.SCATTER, group_by=GroupBy(dimension="enrollment_count"))

    message = only_error(plan, vocab)
    assert "quantitative group_by dimension" in message
    assert "Enrollment is the only quantitative field" in message
    assert "complete_records" in message


# --- rule 4: years -------------------------------------------------------------------------


def test_inverted_year_range(vocab: Vocabulary) -> None:
    plan = a_plan(filters=StudyFilter(start_year=2025, end_year=2015))

    assert "filters.start_year (2025) is after filters.end_year (2015)" in only_error(plan, vocab)


def test_year_out_of_range(vocab: Vocabulary) -> None:
    plan = a_plan(filters=StudyFilter(start_year=1848))

    assert "outside 1900-2100" in only_error(plan, vocab)


def test_series_years_are_checked_too(vocab: Vocabulary) -> None:
    plan = a_plan(
        intent=Intent.COMPARISON,
        series=[
            SeriesSpec(label="A", filters=StudyFilter(start_year=2015, end_year=2020)),
            SeriesSpec(label="B", filters=StudyFilter(start_year=2025, end_year=2020)),
        ],
    )

    assert "series[1].filters.start_year" in only_error(plan, vocab)


def test_a_valid_year_range_passes(vocab: Vocabulary) -> None:
    plan = a_plan(filters=StudyFilter(start_year=2015, end_year=2025))

    assert validate_plan(plan, vocab) == []


# --- rule 5: interpretation states no counts ------------------------------------------------


def test_empty_interpretation(vocab: Vocabulary) -> None:
    assert "interpretation is empty" in only_error(a_plan(interpretation="   "), vocab)


def test_interpretation_stating_a_count(vocab: Vocabulary) -> None:
    plan = a_plan(interpretation="There are 1,750 phase 2 pembrolizumab trials.")

    message = only_error(plan, vocab)
    assert "1,750" in message
    assert "must never state a count" in message


def test_interpretation_with_a_bare_small_number(vocab: Vocabulary) -> None:
    plan = a_plan(interpretation="The top 5 sponsors of clinical trials, by phase.")

    assert "5" in only_error(plan, vocab)


def test_digits_that_are_part_of_a_name_are_not_smuggled_counts(vocab: Vocabulary) -> None:
    """The rule exists to stop invented counts, not to ban the digit 1 from a drug name.

    Every one of these was a 422 for a plan that was correct: `PD-1` and `SARS-CoV-2` name what
    the caller asked about, and `phase 1/2` names two buckets of the axis.
    """
    for interpretation in (
        "Distribution of trials of PD-1 inhibitors across trial phases.",
        "Distribution of SARS-CoV-2 trials across trial phases.",
        "Distribution of phase 1/2 trials across trial phases.",
    ):
        plan = a_plan(interpretation=interpretation)
        assert validate_plan(plan, vocab) == [], interpretation


def test_a_number_next_to_a_counted_thing_is_still_rejected(vocab: Vocabulary) -> None:
    """Relaxing the rule must not relax the thing the rule is for."""
    plan = a_plan(interpretation="Distribution of the 412 trials across trial phases.")

    assert "412" in only_error(plan, vocab)

    plan = a_plan(interpretation="Distribution of 1,204 studies across trial phases.")

    assert "1,204" in only_error(plan, vocab)


def test_years_in_the_interpretation_are_allowed(vocab: Vocabulary) -> None:
    plan = a_plan(
        filters=StudyFilter(start_year=2015, end_year=2025),
        interpretation="Annual count of clinical trials, 2015-2025.",
    )

    assert validate_plan(plan, vocab) == []


def test_digits_inside_a_filter_value_are_allowed(vocab: Vocabulary) -> None:
    """`COVID-19` is not a smuggled count, and rejecting it would be the rule misfiring."""
    plan = a_plan(
        filters=StudyFilter(condition="COVID-19"),
        interpretation="Distribution of clinical trials in COVID-19 across trial phases.",
    )

    assert validate_plan(plan, vocab) == []


def test_digits_inside_an_enum_label_are_allowed(vocab: Vocabulary) -> None:
    """The heuristic planner writes 'Phase 2' from the vocabulary's own label."""
    plan = a_plan(
        filters=StudyFilter(phase=["PHASE2"]),
        interpretation="Distribution of clinical trials in Phase 2 across trial phases.",
    )

    assert validate_plan(plan, vocab) == []


def test_a_count_beside_an_allowed_number_is_still_caught(vocab: Vocabulary) -> None:
    plan = a_plan(
        filters=StudyFilter(condition="COVID-19", start_year=2020),
        interpretation="All 4,821 COVID-19 clinical trials since 2020, by phase.",
    )

    assert "4,821" in only_error(plan, vocab)


def test_overlong_interpretation(vocab: Vocabulary) -> None:
    """Over 300 characters cannot reach `validate_plan`, so the model class holds that line."""
    with pytest.raises(ValidationError):
        a_plan(interpretation="x" * 301)


# --- rule 6: enrollment metrics are plan-valid; runtime refuses above threshold -----------


@pytest.mark.parametrize("metric", [Metric.ENROLLMENT_SUM, Metric.ENROLLMENT_MEDIAN])
def test_enrollment_metrics_pass_plan_validation(metric: Metric, vocab: Vocabulary) -> None:
    """T10: plan-time acceptance; above-threshold refusal lives in the engine, not here."""
    assert validate_plan(a_plan(metric=metric), vocab) == []


def test_study_count_passes(vocab: Vocabulary) -> None:
    assert validate_plan(a_plan(metric=Metric.STUDY_COUNT), vocab) == []


# --- every message is actionable -----------------------------------------------------------


BROKEN_PLANS: list[AnalysisPlan] = [
    a_plan(group_by=GroupBy(dimension="phasez")),
    a_plan(group_by=GroupBy(dimension="phase", bin=Bin(size=5))),
    a_plan(filters=StudyFilter(phase=["PHASE9"])),
    a_plan(filters=StudyFilter(start_year=2025, end_year=2015)),
    a_plan(filters=StudyFilter(start_year=1848)),
    a_plan(intent=Intent.TREND, group_by=GroupBy(dimension="country")),
    a_plan(intent=Intent.GEO),
    a_plan(intent=Intent.COMPARISON),
    a_plan(intent=Intent.SCATTER),
    a_plan(intent=Intent.NETWORK, metric=Metric.ENROLLMENT_SUM),
    a_plan(interpretation="There are 1,750 trials."),
]


@pytest.mark.parametrize("plan", BROKEN_PLANS, ids=range(len(BROKEN_PLANS)))
def test_every_message_names_a_field_and_an_alternative(
    plan: AnalysisPlan, vocab: Vocabulary
) -> None:
    """These strings are fed verbatim to the model as repair input (SPEC §3)."""
    messages = validate_plan(plan, vocab)
    assert messages

    for message in messages:
        assert message.endswith("."), f"not a sentence: {message}"
        assert len(message) > 40, f"too terse to act on: {message}"
        assert any(
            hint in message
            for hint in ("Valid", "Use ", "Set ", "Group by", "Add ", "drop ", "Swap", "must")
        ), f"no repair instruction: {message}"


# --- enforce_hard_constraints (SPEC §2.1) --------------------------------------------------


def test_request_fields_override_planner_filters() -> None:
    plan = a_plan(filters=StudyFilter(intervention="aspirin"))
    request = AnalyzeRequest(query="trials by phase", drug_name="Pembrolizumab")

    enforced, assumptions = enforce_hard_constraints(plan, request)

    assert enforced.filters.intervention == "Pembrolizumab"
    assert len(assumptions) == 1
    assert "aspirin" in assumptions[0]
    assert "Pembrolizumab" in assumptions[0]


def test_absent_request_fields_leave_planner_filters_alone() -> None:
    plan = a_plan(filters=StudyFilter(intervention="Pembrolizumab", condition="Melanoma"))
    request = AnalyzeRequest(query="trials by phase")

    enforced, assumptions = enforce_hard_constraints(plan, request)

    assert enforced.filters.intervention == "Pembrolizumab"
    assert enforced.filters.condition == "Melanoma"
    assert assumptions == []


def test_agreement_is_not_recorded_as_an_override() -> None:
    plan = a_plan(filters=StudyFilter(intervention="Pembrolizumab"))
    request = AnalyzeRequest(query="trials by phase", drug_name="Pembrolizumab")

    enforced, assumptions = enforce_hard_constraints(plan, request)

    assert enforced == plan
    assert assumptions == []


def test_list_filters_are_overridden_wholesale() -> None:
    plan = a_plan(filters=StudyFilter(phase=["PHASE1"]))
    request = AnalyzeRequest(query="trials by phase", phase=["PHASE2", "PHASE3"])

    enforced, assumptions = enforce_hard_constraints(plan, request)

    assert enforced.filters.phase == ["PHASE2", "PHASE3"]
    assert len(assumptions) == 1


def test_empty_request_lists_do_not_clear_planner_filters() -> None:
    plan = a_plan(filters=StudyFilter(phase=["PHASE2"]))
    request = AnalyzeRequest(query="trials by phase", phase=[])

    enforced, assumptions = enforce_hard_constraints(plan, request)

    assert enforced.filters.phase == ["PHASE2"]
    assert assumptions == []


def test_every_structured_field_is_enforceable() -> None:
    """A field the request accepts but enforcement ignores is a filter that silently vanishes."""
    plan = a_plan(filters=StudyFilter())
    request = AnalyzeRequest(
        query="trials by phase",
        drug_name="Pembrolizumab",
        condition="Melanoma",
        sponsor="Merck Sharp & Dohme LLC",
        country="France",
        phase=["PHASE3"],
        status=["RECRUITING"],
        study_type="INTERVENTIONAL",
        start_year=2015,
        end_year=2025,
    )

    enforced, assumptions = enforce_hard_constraints(plan, request)

    assert enforced.filters == StudyFilter(
        intervention="Pembrolizumab",
        condition="Melanoma",
        sponsor="Merck Sharp & Dohme LLC",
        country="France",
        phase=["PHASE3"],
        status=["RECRUITING"],
        study_type="INTERVENTIONAL",
        start_year=2015,
        end_year=2025,
    )
    assert len(assumptions) == 9


def test_a_comparison_cannot_vary_a_field_the_request_pinned() -> None:
    """Both ways of resolving this conflict fabricate, so it is refused instead.

    Honouring the overlays reports Merck's and Pfizer's counts while `filters_applied` says
    Novartis. Stamping Novartis over both draws one number as two differently labelled bars.
    """
    plan = a_plan(
        intent=Intent.COMPARISON,
        series=[
            SeriesSpec(label="Merck", filters=StudyFilter(sponsor="Merck Sharp & Dohme LLC")),
            SeriesSpec(label="Pfizer", filters=StudyFilter(sponsor="Pfizer")),
        ],
    )
    request = AnalyzeRequest(query="compare sponsors by phase", sponsor="Novartis")

    with pytest.raises(CheironError) as caught:
        enforce_hard_constraints(plan, request)

    assert caught.value.code is ErrorCode.UNPLANNABLE_QUERY
    assert len(caught.value.details) == 2
    assert "the request pins sponsor to 'Novartis'" in caught.value.details[0]["message"]


def test_series_overlays_on_unpinned_fields_survive_enforcement() -> None:
    """The hard constraint ANDs into every series; the varied field is still the series' own."""
    plan = a_plan(
        intent=Intent.COMPARISON,
        series=[
            SeriesSpec(label="Merck", filters=StudyFilter(sponsor="Merck Sharp & Dohme LLC")),
            SeriesSpec(label="Pfizer", filters=StudyFilter(sponsor="Pfizer")),
        ],
    )
    request = AnalyzeRequest(query="compare sponsors by phase", drug_name="pembrolizumab")

    enforced, _ = enforce_hard_constraints(plan, request)

    assert [series.filters.sponsor for series in enforced.series] == [
        "Merck Sharp & Dohme LLC",
        "Pfizer",
    ]
    assert enforced.filters.intervention == "pembrolizumab"


def test_overlay_filters_ands_shared_hard_constraints_into_each_series() -> None:
    """drug_name on the request must reach every series query, while overlays still win."""
    from app.planner.validate import overlay_filters

    base = StudyFilter(intervention="Pembrolizumab", sponsor="Novartis")
    merck = StudyFilter(sponsor="Merck Sharp & Dohme LLC")
    pfizer = StudyFilter(sponsor="Pfizer")

    assert overlay_filters(base, merck).intervention == "Pembrolizumab"
    assert overlay_filters(base, merck).sponsor == "Merck Sharp & Dohme LLC"
    assert overlay_filters(base, pfizer).sponsor == "Pfizer"


def test_year_shaped_count_without_a_filter_bound_is_caught(vocab: Vocabulary) -> None:
    plan = a_plan(interpretation="There were 2024 pembrolizumab trials in this set.")

    message = only_error(plan, vocab)
    assert "2024" in message


def test_series_label_with_a_smuggled_count_is_caught(vocab: Vocabulary) -> None:
    plan = a_plan(
        intent=Intent.COMPARISON,
        series=[
            SeriesSpec(label="Merck (1,841)", filters=StudyFilter(sponsor="Merck")),
            SeriesSpec(label="Pfizer", filters=StudyFilter(sponsor="Pfizer")),
        ],
        interpretation="Comparison of clinical trials by sponsor.",
    )

    assert any("1,841" in message for message in validate_plan(plan, vocab))


def test_enrollment_with_secondary_group_by_is_refused(vocab: Vocabulary) -> None:
    plan = a_plan(
        metric=Metric.ENROLLMENT_SUM,
        secondary_group_by=GroupBy(dimension="overall_status"),
    )

    assert any("secondary_group_by" in message for message in validate_plan(plan, vocab))


def test_the_original_plan_is_not_mutated() -> None:
    plan = a_plan(filters=StudyFilter(intervention="aspirin"))
    request = AnalyzeRequest(query="trials by phase", drug_name="Pembrolizumab")

    enforce_hard_constraints(plan, request)

    assert plan.filters.intervention == "aspirin"
