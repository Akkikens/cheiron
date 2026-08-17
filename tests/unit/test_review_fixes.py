"""Regressions found by review, each pinned so it cannot come back.

Every test here corresponds to a defect that shipped and passed the suite at the time. They are
grouped in one file deliberately: the common thread is that all nine were invisible to tests
written from the spec, because each one is a case the spec describes correctly and the code
implemented under a slightly different assumption.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from app.cache import options_cache_key, result_cache_key
from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.engine.basefilter import base_filter, year_span
from app.engine.bucketset import Bucket, BucketSet
from app.engine.context import new_context
from app.engine.coverage import build_coverage
from app.engine.dimensions import REGISTRY
from app.engine.modes import network
from app.engine.preflight import select_mode
from app.errors import CheironError, ErrorCode
from app.models.plan import (
    AnalysisPlan,
    ChartType,
    GroupBy,
    Intent,
    Metric,
    SeriesSpec,
    StudyFilter,
)
from app.models.request import Options
from app.render.encode import render
from tests.conftest import Handler, stub_transport

DATA_TIMESTAMP = "2026-08-14T09:00:05"


@pytest.fixture
async def vocab(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


# --- 1: a closed vocabulary is not the same as "has an enum" --------------------------------


def test_a_large_trend_uses_server_counts_not_sampling() -> None:
    """start_year is closed (a derived range) but carries enum_name=None.

    Routing it to the sampling mode confirmed year labels with COVERAGE[FullMatch]"2021" instead
    of RANGE[2021-01-01,2021-12-31], which confirms to zero and empties the chart — for the most
    commonly asked question there is.
    """
    assert select_mode(50_000, REGISTRY["start_year"], 2_000) == "server_counts"
    assert select_mode(1_500, REGISTRY["start_year"], 2_000) == "complete_records"


def test_open_vocabularies_still_sample() -> None:
    assert select_mode(50_000, REGISTRY["lead_sponsor"], 2_000) == "sampled_then_confirmed"
    assert select_mode(50_000, REGISTRY["phase"], 2_000) == "server_counts"


# --- 2: the result cache key must include response-shaping options --------------------------


def test_options_change_the_result_cache_key() -> None:
    """Without this, an explain=false caller could be served a cached plan and query log."""
    plain = options_cache_key(Options())
    explained = options_cache_key(Options(explain=True))
    narrow = options_cache_key(Options(max_buckets=3))
    uncited = options_cache_key(Options(include_citations=False))

    assert len({plain, explained, narrow, uncited}) == 4
    assert result_cache_key("p", DATA_TIMESTAMP, plain) != result_cache_key(
        "p", DATA_TIMESTAMP, explained
    )


def test_the_same_question_and_options_still_share_a_key() -> None:
    assert result_cache_key("p", DATA_TIMESTAMP, options_cache_key(Options())) == result_cache_key(
        "p", DATA_TIMESTAMP, options_cache_key(Options())
    )


# --- 3: a future start_year is a valid question, not a 500 ----------------------------------


def test_a_future_start_year_does_not_invert_the_range() -> None:
    """`start_year=2030` with no end asks about planned trials; it used to raise into a 500."""
    span = year_span(StudyFilter(start_year=2030))

    assert span is not None
    start, end = span
    assert start == date(2030, 1, 1)
    assert end >= start

    params, _ = base_filter(StudyFilter(start_year=2030))
    assert "filter.advanced" in params


def test_an_explicit_end_year_still_wins() -> None:
    span = year_span(StudyFilter(start_year=2015, end_year=2020))

    assert span == (date(2015, 1, 1), date(2020, 12, 31))


# --- 5 and 6: network accounting ------------------------------------------------------------


def intervention_study(nct: str, names: list[str]) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct},
            "armsInterventionsModule": {
                "interventions": [{"name": name, "type": "DRUG"} for name in names]
            },
        }
    }


async def a_ctx(settings: Settings, vocab: Vocabulary, max_buckets: int = 20) -> Any:
    return new_context(
        CTGClient(stub_transport(settings, lambda _r: httpx.Response(404))),
        vocab,
        Options(include_citations=False, max_buckets=max_buckets),
        settings=settings,
        data_timestamp=DATA_TIMESTAMP,
    )


def a_network_plan() -> AnalysisPlan:
    return AnalysisPlan(
        intent=Intent.NETWORK,
        filters=StudyFilter(intervention="pembrolizumab"),
        group_by=GroupBy(dimension="intervention_name"),
        interpretation="Co-occurrence of interventions across trials.",
    )


async def test_a_node_weight_is_trials_not_twice_trials(
    settings: Settings, vocab: Vocabulary
) -> None:
    """left and right are the same list in the intervention pairing; both loops counted it."""
    ctx = await a_ctx(settings, vocab)
    studies = [
        intervention_study("NCT1", ["A", "B"]),
        intervention_study("NCT2", ["A", "B"]),
        intervention_study("NCT3", ["C", "D"]),
    ]

    viz, _ = network.build(studies, a_network_plan(), ctx, pairing="intervention_intervention")

    weights = {node["id"]: node["weight"] for node in viz.data["nodes"]}
    assert weights["A"] == 2  # two trials, not four
    assert weights["B"] == 2


async def test_the_prune_annotation_counts_every_hidden_edge(
    settings: Settings, vocab: Vocabulary
) -> None:
    """A disclosure reading "1 of 1 edges" while hiding two is the failure it exists to prevent."""
    ctx = await a_ctx(settings, vocab, max_buckets=2)
    studies = [
        intervention_study("NCT1", ["A", "B"]),
        intervention_study("NCT2", ["A", "B"]),
        intervention_study("NCT3", ["C", "D"]),
        intervention_study("NCT4", ["C", "D"]),
        intervention_study("NCT5", ["E", "F"]),
        intervention_study("NCT6", ["E", "F"]),
    ]

    viz, _ = network.build(studies, a_network_plan(), ctx, pairing="intervention_intervention")

    annotation = next(a for a in (viz.annotations or []) if a["type"] == "prune")
    shown_edges = len(viz.data["edges"])

    assert annotation["shown_edges"] == shown_edges
    assert annotation["total_edges_before_prune"] == 3
    assert annotation["edges_dropped_with_pruned_nodes"] == 3 - shown_edges
    assert annotation["total_nodes"] == 6


# --- 7: coverage arithmetic is about study counts -------------------------------------------


def a_bucketset(values: dict[str, float], total: int, unclassified: int = 0) -> BucketSet:
    return BucketSet(
        buckets=[
            Bucket(key=key, label=key, value=value, exactness="exact")
            for key, value in values.items()
        ],
        total=total,
        unclassified=unclassified,
        semantics="partition",
        mode="complete_records",
    )


def test_enrollment_coverage_does_not_claim_an_unexplained_difference() -> None:
    """bucket_sum is people and total is studies; reconciling them reads as a data fault."""
    bucketset = a_bucketset({"INTERVENTIONAL": 1_204_331.0}, total=1_840, unclassified=12)

    coverage, warnings = build_coverage(bucketset, REGISTRY["study_type"], counts_studies=False)

    assert not any("unexplained" in warning for warning in warnings)
    assert coverage.overlap_note is not None
    assert "enrollment totals, not study counts" in coverage.overlap_note
    assert "1,840 studies" in coverage.overlap_note


def test_study_count_coverage_still_reconciles() -> None:
    bucketset = a_bucketset({"INTERVENTIONAL": 1_800.0}, total=1_840, unclassified=12)

    _, warnings = build_coverage(bucketset, REGISTRY["study_type"], counts_studies=True)

    assert any("does not equal" in warning for warning in warnings)


# --- 9: a 429 is upstream telling us the rate -----------------------------------------------


async def test_a_429_surfaces_as_rate_limited_with_retry_after(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow down", headers={"Retry-After": "7"})

    client = CTGClient(stub_transport(settings, handler))

    with pytest.raises(CheironError) as caught:
        await client.count({"query.cond": "cancer"})

    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.status == 429
    assert caught.value.retry_after_seconds == 7
    assert calls["n"] > 1  # still retried, just politely


async def test_a_429_without_a_header_still_reports_rate_limited(settings: Settings) -> None:
    client = CTGClient(stub_transport(settings, lambda _r: httpx.Response(429, text="slow")))

    with pytest.raises(CheironError) as caught:
        await client.count({"query.cond": "cancer"})

    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.retry_after_seconds is None


# --- 10: an empty network result is still a network graph -----------------------------------


async def test_an_empty_network_result_keeps_its_shape(
    settings: Settings, vocab: Vocabulary
) -> None:
    """Returning TABLE with {nodes, edges} fails the response model and 500s an empty answer."""
    ctx = await a_ctx(settings, vocab)
    empty = BucketSet(
        buckets=[],
        total=0,
        unclassified=0,
        semantics="overlapping",
        mode="complete_records",
    )

    viz, warnings = render(
        a_network_plan(), empty, ChartType.NETWORK_GRAPH, REGISTRY["intervention_name"], ctx
    )

    assert viz.type is ChartType.NETWORK_GRAPH
    assert viz.data == {"nodes": [], "edges": []}
    assert warnings


async def test_an_empty_bar_chart_still_collapses_to_a_table(
    settings: Settings, vocab: Vocabulary
) -> None:
    ctx = await a_ctx(settings, vocab)
    empty = BucketSet(
        buckets=[], total=0, unclassified=0, semantics="overlapping", mode="server_counts"
    )

    viz, _ = render(
        AnalysisPlan(
            intent=Intent.DISTRIBUTION,
            filters=StudyFilter(),
            group_by=GroupBy(dimension="phase"),
            metric=Metric.STUDY_COUNT,
            interpretation="Distribution across phases.",
        ),
        empty,
        ChartType.BAR_CHART,
        REGISTRY["phase"],
        ctx,
    )

    assert viz.type is ChartType.TABLE
    assert viz.data == []


# --- network citations must be checkable ----------------------------------------------------


async def test_edge_citations_quote_the_record_not_the_id(
    settings: Settings, vocab: Vocabulary
) -> None:
    """Repeating the NCT id as the excerpt cites nothing — it restates `nct_id` (SPEC §4.2)."""
    ctx = await a_ctx(settings, vocab)
    ctx.options = Options(include_citations=True, citations_per_datum=2)
    studies = [
        intervention_study("NCT1", ["Temozolomide", "Bevacizumab"]),
        intervention_study("NCT2", ["Temozolomide", "Bevacizumab"]),
    ]

    viz, _ = network.build(studies, a_network_plan(), ctx, pairing="intervention_intervention")

    citation = viz.data["edges"][0]["citations"][0]
    assert citation["nct_id"] == "NCT1"
    assert citation["excerpt"] != citation["nct_id"]
    assert "Temozolomide" in citation["excerpt"]
    assert "interventions" in citation["field"]
    assert "briefSummary" not in citation["field"]


# --- enrollment_count: the dimension that makes histogram and scatter reachable --------------


def test_enrollment_bins_are_trial_shaped_not_equal_width() -> None:
    """Enrollment spans 0 to 188,814,085; linear bins would put nearly every study in the first."""
    from app.engine.dimensions import ENROLLMENT_BINS, bin_key

    assert bin_key(0) == "0-10"
    assert bin_key(45) == "11-50"
    assert bin_key(640) == "501-1,000"
    assert bin_key(188_814_085) == "5,001+"  # the outlier lands in the open top bin
    assert ENROLLMENT_BINS[-1][1] is None


def test_a_missing_enrollment_is_unclassified_not_bin_zero() -> None:
    """notes §6.4: enrollment is missing on 7,133 studies.

    Folding those into the 0-10 bin invents a spike of tiny trials that does not exist.
    """
    from app.engine.modes.records import membership_keys

    dim = REGISTRY["enrollment_count"]
    present = {"protocolSection": {"designModule": {"enrollmentInfo": {"count": 40}}}}
    absent = {"protocolSection": {"designModule": {}}}

    assert membership_keys(present, dim) == ["11-50"]
    assert membership_keys(absent, dim) is None


async def test_a_histogram_is_ordered_by_bin_not_by_height(
    settings: Settings, vocab: Vocabulary
) -> None:
    """A histogram sorted by count is a bar chart wearing a histogram's axis."""
    ctx = await a_ctx(settings, vocab)
    buckets = [
        Bucket(key="501-1,000", label="501-1,000", value=7, exactness="exact"),
        Bucket(key="11-50", label="11-50", value=107, exactness="exact"),
        Bucket(key="0-10", label="0-10", value=31, exactness="exact"),
    ]
    bucketset = BucketSet(
        buckets=buckets, total=145, unclassified=0, semantics="partition", mode="server_counts"
    )
    plan = AnalysisPlan(
        intent=Intent.HISTOGRAM,
        filters=StudyFilter(condition="glioblastoma"),
        group_by=GroupBy(dimension="enrollment_count"),
        interpretation="Distribution of trials by enrollment size.",
    )

    viz, _ = render(plan, bucketset, ChartType.HISTOGRAM, REGISTRY["enrollment_count"], ctx)

    assert [row["enrollment_count"] for row in viz.data] == ["0-10", "11-50", "501-1,000"]
    assert viz.data[0]["bin_start"] == 0 and viz.data[0]["bin_end"] == 10
    assert viz.encoding["x"]["bin_start"] == "bin_start"


# --- capped bucket lists are not "unexplained" ----------------------------------------------


def test_a_capped_partition_is_not_reported_as_an_unexplained_difference() -> None:
    """Showing 3 of 51,610 sponsors is expected not to reconcile — the cap is the explanation.

    Warning that the difference is unexplained points at the data when it should point at
    options.max_buckets, and it fired on a live query for exactly that reason.
    """
    capped = BucketSet(
        buckets=[
            Bucket(key=name, label=name, value=value, exactness="exact")
            for name, value in (("A", 200), ("B", 120), ("C", 72))
        ],
        total=2_927,
        unclassified=0,
        semantics="partition",
        # server_counts, because the response model rightly refuses a sampled mode with no
        # sample_size — the cap being tested here is max_buckets, not sampling.
        mode="server_counts",
        complete=False,
    )

    coverage, warnings = build_coverage(capped, REGISTRY["lead_sponsor"])

    assert not any("unexplained" in warning for warning in warnings)
    assert coverage.overlap_note is not None
    assert "options.max_buckets" in coverage.overlap_note
    assert "2,927" in coverage.overlap_note


def test_a_complete_partition_that_does_not_reconcile_still_warns() -> None:
    """The bug this warning exists for has not been softened away."""
    complete = BucketSet(
        buckets=[
            Bucket(key="INTERVENTIONAL", label="Interventional", value=1_800, exactness="exact")
        ],
        total=1_840,
        unclassified=12,
        semantics="partition",
        mode="complete_records",
        complete=True,
    )

    _, warnings = build_coverage(complete, REGISTRY["study_type"])

    assert any("does not equal" in warning for warning in warnings)


# --- stacking reads the secondary dimension --------------------------------------------------


def test_stacking_is_decided_by_the_secondary_dimension() -> None:
    """Reading the primary's flag got both directions backwards on live data."""
    from app.render.registry import select_chart

    def chart(primary: str, secondary: str) -> Any:
        plan = AnalysisPlan(
            intent=Intent.DISTRIBUTION,
            filters=StudyFilter(),
            group_by=GroupBy(dimension=primary),
            secondary_group_by=GroupBy(dimension=secondary),
            interpretation="Cross-tab of two dimensions.",
        )
        bucketset = BucketSet(
            buckets=[Bucket(key="k", label="k", value=1, exactness="exact")],
            total=1,
            unclassified=0,
            semantics="partition",
            mode="complete_records",
        )
        return select_chart(plan, bucketset, REGISTRY[primary], Options())[0]

    # One status per study, so phase-by-status segments genuinely sum to their bar.
    assert chart("phase", "overall_status") is ChartType.STACKED_BAR_CHART
    # A study can hold two phases, so status-by-phase segments do not.
    assert chart("overall_status", "phase") is ChartType.GROUPED_BAR_CHART


# --- second review pass: plan coherence, transport, histogram bins ---------------------------


async def test_a_lone_series_is_refused_rather_than_ignored(vocab: Vocabulary) -> None:
    """A one-element series took the single-series path, where its filters were never read."""
    from app.planner.validate import validate_plan

    plan = AnalysisPlan(
        intent=Intent.DISTRIBUTION,
        filters=StudyFilter(),
        group_by=GroupBy(dimension="phase"),
        series=[],
        interpretation="Distribution across phases.",
    )
    lone = plan.model_copy(update={"series": [SeriesSpec(label="A", filters=StudyFilter())]})

    assert validate_plan(plan, vocab) == []
    messages = validate_plan(lone, vocab)
    assert any("single series is not a comparison" in message for message in messages)


async def test_series_and_secondary_together_are_refused(vocab: Vocabulary) -> None:
    """A grouped bar has one breakdown channel; the secondary was silently dropped."""
    from app.planner.validate import validate_plan

    plan = AnalysisPlan(
        intent=Intent.COMPARISON,
        filters=StudyFilter(),
        group_by=GroupBy(dimension="phase"),
        secondary_group_by=GroupBy(dimension="overall_status"),
        series=[
            SeriesSpec(label="A", filters=StudyFilter()),
            SeriesSpec(label="B", filters=StudyFilter()),
        ],
        interpretation="Comparison across two sponsors.",
    )

    messages = validate_plan(plan, vocab)

    assert any("cannot show two" in message for message in messages)


def test_retry_after_is_reported_honestly_and_survives_a_malformed_header() -> None:
    """The value handed to the caller is upstream's own, uncapped.

    Clamping it would tell a well-behaved client to retry in 30s when upstream asked for an
    hour, so it retries too early and is limited again. The ceiling belongs on our own sleep.
    """
    from app.ctg.client import _retry_after_seconds

    def header(value: str) -> Any:
        return httpx.Response(429, headers={"Retry-After": value})

    assert _retry_after_seconds(header("7")) == 7
    assert _retry_after_seconds(header("3600")) == 3600
    assert _retry_after_seconds(header("inf")) is None  # int(float("inf")) used to raise
    assert _retry_after_seconds(header("nan")) is None
    assert _retry_after_seconds(header("Wed, 21 Oct 2026 07:28:00 GMT")) is None


async def test_a_long_retry_after_is_slept_only_within_the_request_budget(
    settings: Settings,
) -> None:
    """A flat 30s ceiling was three times the 10s default budget, per attempt."""
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow", headers={"Retry-After": "3600"})

    client = CTGClient(stub_transport(settings, handler, sleep=record))

    with pytest.raises(CheironError) as caught:
        await client.count({"query.cond": "cancer"})

    budget_s = settings.request_budget_ms / 1000
    assert slept, "a 429 should be retried at all"
    assert max(slept) <= budget_s / 2
    # The caller is still told what upstream actually asked for.
    assert caught.value.retry_after_seconds == 3600


async def test_a_histogram_never_produces_an_other_bar(
    settings: Settings, vocab: Vocabulary
) -> None:
    """`_bin_edges("OTHER")` returned [0, inf) — a full-width bar overlapping every real one."""
    ctx = await a_ctx(settings, vocab)
    ctx.options = Options(include_citations=False, max_buckets=3)
    buckets = [
        Bucket(key=key, label=key, value=value, exactness="exact")
        for key, value in (
            ("0-10", 31),
            ("11-50", 107),
            ("51-100", 38),
            ("101-500", 45),
            ("501-1,000", 7),
        )
    ]
    bucketset = BucketSet(
        buckets=buckets, total=228, unclassified=0, semantics="partition", mode="complete_records"
    )
    plan = AnalysisPlan(
        intent=Intent.HISTOGRAM,
        filters=StudyFilter(),
        group_by=GroupBy(dimension="enrollment_count"),
        interpretation="Distribution by enrollment size.",
    )

    viz, _ = render(plan, bucketset, ChartType.HISTOGRAM, REGISTRY["enrollment_count"], ctx)

    assert "OTHER" not in {row["enrollment_count"] for row in viz.data}
    assert all(row["bin_end"] is not None for row in viz.data)
    # The axis is ordinal: `field` holds a bin label, and the numbers live in the edges.
    assert viz.encoding["x"]["type"] == "ordinal"


# --- fourth review pass ------------------------------------------------------------------------


def test_the_how_many_people_keyword_family_stays_banned() -> None:
    """Three attempts at this keyword family produced three shadowing bugs.

    "how many participants" stole "…in each phase"; "how many patients" stole it again; and the
    narrower "how many patients are in" / "how many patients per" stole "How many patients are
    in each phase?" and "How many patients per phase?" — the latter unfixable in principle,
    since "per <dimension>" is a continuation by construction.

    Counting people is an enrollment_sum question this planner cannot express. It answers the
    *distribution* of trial sizes, so only size phrasings are keywords, and a question about
    patient counts is unplannable here rather than answered with the wrong chart.
    """
    from app.planner.heuristic import match

    for stolen in (
        "How many patients are enrolled in each phase?",
        "How many participants are enrolled in each phase?",
        "How many patients are in each phase?",
        "How many patients per phase?",
    ):
        assert match(stolen).key == "phase", stolen

    assert match("How many patients are in trials by country?").key == "country"

    # Size phrasings still work; a bare people-count does not, and that is the honest answer.
    assert match("How big are these trials?").key == "enrollment"
    assert match("What is the typical enrollment?").key == "enrollment"
    assert match("How many patients are in these trials?") is None


async def test_retry_sleeps_stay_inside_the_budget_across_every_attempt(
    settings: Settings,
) -> None:
    """Halving the budget still spent all of it: a get() sleeps between every attempt."""
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    client = CTGClient(
        stub_transport(
            settings,
            lambda _r: httpx.Response(429, text="slow", headers={"Retry-After": "3600"}),
            sleep=record,
        )
    )

    with pytest.raises(CheironError):
        await client.count({"query.cond": "cancer"})

    assert sum(slept) <= settings.request_budget_ms / 1000


def test_a_truncation_note_never_claims_a_denominator_it_does_not_have() -> None:
    """ "Showing 3 of 3 values, the rest cut" is a contradiction, and it shipped.

    When the cap bites inside the fan-out rather than at the chart's axis, the values beyond it
    were never counted, so there is no total to quote. Only an axis cut knows both numbers.
    """
    from app.engine.coverage import build_coverage

    def note(shown: int, omitted: int) -> str:
        bucketset = BucketSet(
            buckets=[
                Bucket(key=f"K{i}", label=f"K{i}", value=10, exactness="exact")
                for i in range(shown)
            ],
            total=500,
            unclassified=0,
            semantics="partition",
            mode="server_counts",
            complete=False,
            omitted_buckets=omitted,
        )
        return build_coverage(bucketset, REGISTRY["lead_sponsor"])[0].overlap_note or ""

    axis_cut = note(shown=3, omitted=7)
    assert "Showing 3 of 10" in axis_cut

    fanout_cut = note(shown=3, omitted=0)
    assert "Showing 3 of 3" not in fanout_cut
    assert "their number is unknown" in fanout_cut


def test_a_narrowed_result_never_prints_a_negative_overlap() -> None:
    """ "overlap -606" shipped, in the one block whose purpose is auditable arithmetic.

    Narrowing the bucket set to the plotted categories moved `bucket_sum` without moving
    `with_value`. Where the memberships counted fall short of the studies carrying a value, the
    difference is not an overlap — it is the part that was never counted — and the zero branch
    would have asserted "no study carries more than one phase", a claim invented by truncation.
    """
    from app.engine.coverage import build_coverage

    full = BucketSet(
        buckets=[
            Bucket(key=f"K{i}", label=f"K{i}", value=100 - i, exactness="exact") for i in range(10)
        ],
        total=1_000,
        unclassified=0,
        semantics="overlapping",
        mode="server_counts",
    )

    note = build_coverage(full.plotted_only({"K0", "K1", "K2", "K3"}), REGISTRY["phase"])[0]

    assert note.overlap_note is not None
    assert "overlap -" not in note.overlap_note
    assert "cannot be quantified" in note.overlap_note
    assert "no study in this result set carries more than one" not in note.overlap_note


def test_a_real_overlap_is_still_quantified() -> None:
    """The A1 shape must keep its numbers; the guard is for incomplete lists only."""
    from app.engine.coverage import build_coverage

    a1 = BucketSet(
        buckets=[
            Bucket(key=key, label=key, value=value, exactness="exact")
            for key, value in (
                ("NA", 53),
                ("EARLY_PHASE1", 51),
                ("PHASE1", 1039),
                ("PHASE2", 1750),
                ("PHASE3", 363),
                ("PHASE4", 17),
            )
        ],
        total=2_927,
        unclassified=169,
        semantics="overlapping",
        mode="server_counts",
    )

    note = build_coverage(a1, REGISTRY["phase"])[0].overlap_note or ""

    assert "overlap 515" in note
    assert "2,758 studies" in note and "3,273 bucket memberships" in note
