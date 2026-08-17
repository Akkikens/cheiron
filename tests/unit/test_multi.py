"""Comparison series and cross-tabs. SPEC §3, §6.1.

The regression these guard against was live: a two-series comparison returned a
`grouped_bar_chart` whose every row carried the *first* series' label and the *base filter's*
counts — a chart asserting a comparison nobody computed. Each test below fails if that returns.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.essie import Essie
from app.ctg.vocab import Vocabulary
from app.engine.bucketset import Bucket, BucketSet
from app.engine.context import new_context
from app.engine.dimensions import REGISTRY
from app.engine.multi import (
    MAX_SERIES,
    CrossCell,
    Panel,
    crosstab_by_counts,
    crosstab_from_records,
    merge_panels,
    too_many_series,
)
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan, GroupBy, Intent, SeriesSpec, StudyFilter
from app.models.request import Options
from app.render.encode import render_crosstab, render_panels
from tests.conftest import Handler, stub_transport
from tests.unit.test_engine_counts import DATA_TIMESTAMP, Upstream, a1_upstream

PHASE = REGISTRY["phase"]
STATUS = REGISTRY["overall_status"]


@pytest.fixture
async def vocab(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


def study(nct: str, phases: list[str] | None, status: str) -> dict[str, Any]:
    design: dict[str, Any] = {} if phases is None else {"phases": phases}
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct},
            "designModule": design,
            "statusModule": {"overallStatus": status},
        }
    }


def a_plan(**overrides: Any) -> AnalysisPlan:
    base: dict[str, Any] = {
        "intent": Intent.DISTRIBUTION,
        "filters": StudyFilter(),
        "group_by": GroupBy(dimension="phase"),
        "interpretation": "Distribution across phases.",
    }
    return AnalysisPlan(**{**base, **overrides})


def a_panel(label: str, buckets: dict[str, int], total: int) -> Panel:
    return Panel(
        label=label,
        bucketset=BucketSet(
            buckets=[
                Bucket(key=key, label=key, value=value, exactness="exact")
                for key, value in buckets.items()
            ],
            total=total,
            unclassified=0,
            semantics="overlapping",
            mode="server_counts",
        ),
    )


async def a_ctx(settings: Settings, vocab: Vocabulary, upstream: Upstream, budget: int = 40) -> Any:
    return new_context(
        CTGClient(stub_transport(settings, upstream.async_handler)),
        vocab,
        Options(include_citations=False),
        settings=settings.model_copy(update={"max_upstream_requests": budget}),
        data_timestamp=DATA_TIMESTAMP,
    )


# --- comparison -----------------------------------------------------------------------------


async def test_every_row_carries_the_series_it_was_counted_under(
    settings: Settings, vocab: Vocabulary
) -> None:
    """The regression, directly: two series must produce two labels and two sets of counts."""
    panels = [
        a_panel("Merck", {"PHASE2": 400, "PHASE3": 120}, total=520),
        a_panel("Pfizer", {"PHASE2": 900, "PHASE3": 260}, total=1_160),
    ]
    merged, _ = merge_panels(panels, PHASE)
    ctx = await a_ctx(settings, vocab, a1_upstream())

    viz, _ = render_panels(a_plan(intent=Intent.COMPARISON), panels, merged, PHASE, ctx)

    pairs = {(row["phase"], row["series"]): row["study_count"] for row in viz.data}
    assert pairs == {
        ("PHASE2", "Merck"): 400,
        ("PHASE3", "Merck"): 120,
        ("PHASE2", "Pfizer"): 900,
        ("PHASE3", "Pfizer"): 260,
    }
    assert {row["series"] for row in viz.data} == {"Merck", "Pfizer"}


async def test_the_series_annotation_reports_each_series_total(
    settings: Settings, vocab: Vocabulary
) -> None:
    panels = [
        a_panel("Merck", {"PHASE2": 400}, total=520),
        a_panel("Pfizer", {"PHASE2": 900}, total=1_160),
    ]
    merged, _ = merge_panels(panels, PHASE)
    ctx = await a_ctx(settings, vocab, a1_upstream())

    viz, _ = render_panels(a_plan(intent=Intent.COMPARISON), panels, merged, PHASE, ctx)

    annotation = next(a for a in (viz.annotations or []) if a["type"] == "series")
    assert annotation["series"] == [
        {"label": "Merck", "total_matching_studies": 520},
        {"label": "Pfizer", "total_matching_studies": 1_160},
    ]


def test_summed_series_totals_are_disclosed_as_not_a_population_count() -> None:
    """A study matching two series is counted twice; the total says so rather than pretending."""
    panels = [
        a_panel("Merck", {"PHASE2": 400}, total=520),
        a_panel("Pfizer", {"PHASE2": 900}, total=1_160),
    ]

    merged, warnings = merge_panels(panels, PHASE)

    assert merged.total == 1_680
    disclosure = next(w for w in warnings if "counted once per series" in w)
    assert "Merck" in disclosure and "Pfizer" in disclosure


async def test_an_empty_series_is_named_rather_than_dropped(
    settings: Settings, vocab: Vocabulary
) -> None:
    panels = [a_panel("Merck", {"PHASE2": 400}, total=400), a_panel("Ghost", {}, total=0)]
    merged, _ = merge_panels(panels, PHASE)
    ctx = await a_ctx(settings, vocab, a1_upstream())

    _, warnings = render_panels(a_plan(intent=Intent.COMPARISON), panels, merged, PHASE, ctx)

    assert any("'Ghost'" in warning and "no studies" in warning for warning in warnings)


def test_more_series_than_a_grouped_bar_can_carry_is_refused() -> None:
    error = too_many_series(7)

    assert error.code is ErrorCode.UNPLANNABLE_QUERY
    assert "7" in error.message
    assert str(MAX_SERIES) in error.message


# --- cross-tab from records (free) ----------------------------------------------------------


def test_crosstab_from_records_counts_real_pairs() -> None:
    studies = [
        study("NCT1", ["PHASE2"], "RECRUITING"),
        study("NCT2", ["PHASE2"], "RECRUITING"),
        study("NCT3", ["PHASE2"], "COMPLETED"),
        study("NCT4", ["PHASE3"], "COMPLETED"),
    ]

    cells = crosstab_from_records(studies, PHASE, STATUS)

    assert set(cells) == {
        CrossCell("PHASE2", "RECRUITING", 2),
        CrossCell("PHASE2", "COMPLETED", 1),
        CrossCell("PHASE3", "COMPLETED", 1),
    }


def test_a_multi_valued_primary_contributes_to_every_pair() -> None:
    """notes §6.2: a study with two phases is in both, in the cross-tab as well as the bar."""
    cells = crosstab_from_records(
        [study("NCT1", ["PHASE1", "PHASE2"], "RECRUITING")], PHASE, STATUS
    )

    assert set(cells) == {
        CrossCell("PHASE1", "RECRUITING", 1),
        CrossCell("PHASE2", "RECRUITING", 1),
    }


def test_a_study_missing_either_dimension_is_not_invented_into_a_cell() -> None:
    cells = crosstab_from_records([study("NCT1", None, "RECRUITING")], PHASE, STATUS)

    assert cells == []


# --- cross-tab by counts (paid) -------------------------------------------------------------


async def test_crosstab_by_counts_issues_one_count_per_cell(
    settings: Settings, vocab: Vocabulary
) -> None:
    predicates = {
        Essie.and_(
            Essie.field_eq("Phase", "PHASE2"), Essie.field_eq("OverallStatus", "RECRUITING")
        ): 40,
        Essie.and_(
            Essie.field_eq("Phase", "PHASE2"), Essie.field_eq("OverallStatus", "COMPLETED")
        ): 60,
        Essie.and_(
            Essie.field_eq("Phase", "PHASE3"), Essie.field_eq("OverallStatus", "RECRUITING")
        ): 10,
        Essie.and_(
            Essie.field_eq("Phase", "PHASE3"), Essie.field_eq("OverallStatus", "COMPLETED")
        ): 0,
        None: 110,
    }
    upstream = Upstream(predicates)
    ctx = await a_ctx(settings, vocab, upstream)

    cells = await crosstab_by_counts(
        a_plan(),
        PHASE,
        STATUS,
        ctx,
        params={},
        primary_keys=["PHASE2", "PHASE3"],
        secondary_keys=["RECRUITING", "COMPLETED"],
    )

    assert set(cells) == {
        CrossCell("PHASE2", "RECRUITING", 40),
        CrossCell("PHASE2", "COMPLETED", 60),
        CrossCell("PHASE3", "RECRUITING", 10),
    }
    # A zero cell is dropped rather than plotted as an empty segment.
    assert all(cell.value > 0 for cell in cells)


async def test_an_unaffordable_crosstab_refuses_with_the_arithmetic(
    settings: Settings, vocab: Vocabulary
) -> None:
    """Truncating the secondary dimension would make segments not add up to their bar."""
    ctx = await a_ctx(settings, vocab, a1_upstream(), budget=6)

    with pytest.raises(CheironError) as caught:
        await crosstab_by_counts(
            a_plan(),
            PHASE,
            STATUS,
            ctx,
            params={},
            primary_keys=["PHASE1", "PHASE2", "PHASE3"],
            secondary_keys=["RECRUITING", "COMPLETED", "TERMINATED"],
        )

    assert caught.value.code is ErrorCode.UNPLANNABLE_QUERY
    assert "9" in caught.value.message  # 3 x 3 cells required
    assert caught.value.details[0]["cells_required"] == 9


# --- cross-tab rendering --------------------------------------------------------------------


async def test_crosstab_rows_carry_a_real_secondary_value(
    settings: Settings, vocab: Vocabulary
) -> None:
    from app.models.plan import ChartType

    ctx = await a_ctx(settings, vocab, a1_upstream())
    cells = [
        CrossCell("PHASE2", "RECRUITING", 40),
        CrossCell("PHASE2", "COMPLETED", 60),
    ]
    bucketset = BucketSet(
        buckets=[Bucket(key="PHASE2", label="Phase 2", value=100, exactness="exact")],
        total=100,
        unclassified=0,
        semantics="overlapping",
        mode="server_counts",
    )

    viz, _ = render_crosstab(
        a_plan(secondary_group_by=GroupBy(dimension="overall_status")),
        cells,
        bucketset,
        PHASE,
        STATUS,
        ctx,
        ChartType.STACKED_BAR_CHART,
    )

    stacks = {row["stack"] for row in viz.data}
    assert stacks == {"Recruiting", "Completed"}
    assert "all" not in stacks  # the constant that used to stand in for a real breakdown
    assert viz.encoding["stack"]["label"] == "Recruitment status"


async def test_a_multi_valued_secondary_warns_that_segments_do_not_sum(
    settings: Settings, vocab: Vocabulary
) -> None:
    from app.models.plan import ChartType

    ctx = await a_ctx(settings, vocab, a1_upstream())
    cells = [CrossCell("RECRUITING", "PHASE2", 40)]
    bucketset = BucketSet(
        buckets=[Bucket(key="RECRUITING", label="Recruiting", value=40, exactness="exact")],
        total=40,
        unclassified=0,
        semantics="partition",
        mode="server_counts",
    )

    viz, _ = render_crosstab(
        a_plan(group_by=GroupBy(dimension="overall_status")),
        cells,
        bucketset,
        STATUS,
        PHASE,
        ctx,
        ChartType.GROUPED_BAR_CHART,
    )

    assert any("do not sum to their bar" in a.get("text", "") for a in viz.annotations or [])


def test_series_spec_shape_is_what_the_engine_consumes() -> None:
    """Guards the plan-side contract the comparison path depends on."""
    spec = SeriesSpec(label="Merck", filters=StudyFilter(sponsor="Merck Sharp & Dohme LLC"))

    assert spec.label == "Merck"
    assert spec.filters.sponsor == "Merck Sharp & Dohme LLC"


# --- second review pass: fabrication, silent drops, and caps ---------------------------------


async def test_a_comparison_of_enrollment_is_refused_not_answered_with_study_counts(
    settings: Settings, vocab: Vocabulary
) -> None:
    """The worst bug of the second review: study counts shipped labelled "participants".

    The single-series path guards enrollment metrics after preflight; a comparison has one
    preflight per series, so without a guard the counts fan-out returned study counts that
    encode then labelled "Total enrollment" with unit "participants".
    """
    from app.analyze import analyze
    from app.ctg.vocab import VocabularyCache
    from app.errors import CheironError, ErrorCode
    from app.models.plan import Metric
    from app.models.request import AnalyzeRequest

    upstream = a1_upstream()
    plan_payload = a_plan(
        intent=Intent.COMPARISON,
        metric=Metric.ENROLLMENT_SUM,
        series=[
            SeriesSpec(label="A", filters=StudyFilter(sponsor="A")),
            SeriesSpec(label="B", filters=StudyFilter(sponsor="B")),
        ],
    )

    async def completer(messages: Any, schema: Any) -> str:
        import json

        return json.dumps(plan_payload.model_dump(mode="json"))

    with pytest.raises(CheironError) as caught:
        await analyze(
            AnalyzeRequest(query="compare enrollment by sponsor"),
            transport=stub_transport(settings, upstream.async_handler),
            vocabulary_cache=VocabularyCache(),
            settings=settings.model_copy(update={"llm_enabled": True, "openai_api_key": "sk-test"}),
            completer=completer,
        )

    assert caught.value.code is ErrorCode.UNPLANNABLE_QUERY
    assert "enrollment" in caught.value.message.lower()


def test_a_mixed_mode_comparison_reports_the_least_exact_mode() -> None:
    """Series pick their own mode, so claiming the first one's misstates half the bars."""
    exact = a_panel("Small", {"PHASE2": 10}, total=10)
    sampled = Panel(
        label="Large",
        bucketset=BucketSet(
            buckets=[Bucket(key="PHASE2", label="PHASE2", value=900, exactness="exact")],
            total=90_000,
            unclassified=0,
            semantics="overlapping",
            mode="sampled_then_confirmed",
            sample_size=3_000,
            sample_coverage=0.033,
        ),
    )
    exact = Panel(label="Small", bucketset=exact.bucketset)

    merged, warnings = merge_panels([exact, sampled], PHASE)

    assert merged.mode == "sampled_then_confirmed"
    # 3,000 sampled + the 10 the exact series accounted for: an exactly counted study is not an
    # uninspected one, and excluding it understated exactness as badly as inflating it misleads.
    assert merged.sample_size == 3_010
    assert any("different aggregation modes" in warning for warning in warnings)


async def test_a_comparison_respects_max_buckets_across_series(
    settings: Settings, vocab: Vocabulary
) -> None:
    """N series share one axis, so the cap applies to the union of their keys."""
    from app.models.request import Options

    panels = [
        a_panel("A", {f"K{i:02d}": 100 - i for i in range(10)}, total=1_000),
        a_panel("B", {f"K{i:02d}": 50 - i for i in range(10)}, total=500),
    ]
    merged, _ = merge_panels(panels, PHASE)
    ctx = await a_ctx(settings, vocab, a1_upstream())
    ctx.options = Options(include_citations=False, max_buckets=3)

    viz, _ = render_panels(a_plan(intent=Intent.COMPARISON), panels, merged, PHASE, ctx)

    assert {row["phase"] for row in viz.data} == {"K00", "K01", "K02"}
    assert len(viz.data) == 6  # three categories, both series each
    rollup = next(a for a in (viz.annotations or []) if a["type"] == "rollup")
    assert rollup["rolled_categories"] == 7


async def test_a_crosstab_respects_max_buckets(settings: Settings, vocab: Vocabulary) -> None:
    from app.models.plan import ChartType
    from app.models.request import Options

    ctx = await a_ctx(settings, vocab, a1_upstream())
    ctx.options = Options(include_citations=False, max_buckets=2)
    cells = [CrossCell(primary=f"P{i}", secondary="RECRUITING", value=100 - i) for i in range(6)]
    bucketset = BucketSet(
        buckets=[Bucket(key="P0", label="P0", value=100, exactness="exact")],
        total=600,
        unclassified=0,
        semantics="partition",
        mode="complete_records",
    )

    viz, _ = render_crosstab(
        a_plan(secondary_group_by=GroupBy(dimension="overall_status")),
        cells,
        bucketset,
        PHASE,
        STATUS,
        ctx,
        ChartType.STACKED_BAR_CHART,
    )

    assert {row["phase"] for row in viz.data} == {"P0", "P1"}
    rollup = next(a for a in (viz.annotations or []) if a["type"] == "rollup")
    assert rollup["rolled_categories"] == 4


def test_merged_sample_coverage_describes_one_population() -> None:
    """A mean of two ratios claimed 45.5% inspected where the true figure was 1.9%."""
    big = Panel(
        label="Big",
        bucketset=BucketSet(
            buckets=[Bucket(key="PHASE2", label="PHASE2", value=900, exactness="exact")],
            total=100_000,
            unclassified=0,
            semantics="overlapping",
            mode="sampled_then_confirmed",
            sample_size=1_000,
            sample_coverage=0.01,
        ),
    )
    small = Panel(
        label="Small",
        bucketset=BucketSet(
            buckets=[Bucket(key="PHASE2", label="PHASE2", value=90, exactness="exact")],
            total=1_000,
            unclassified=0,
            semantics="overlapping",
            mode="sampled_then_confirmed",
            sample_size=900,
            sample_coverage=0.9,
        ),
    )

    merged, _ = merge_panels([big, small], PHASE)

    assert merged.sample_size == 1_900
    assert merged.total == 101_000
    assert merged.sample_coverage == round(1_900 / 101_000, 3)  # 0.019, not 0.455


def test_a_record_mode_series_counts_as_fully_inspected() -> None:
    """It read every record, so its studies belong in the numerator of the disclosure."""
    read_fully = Panel(
        label="Read",
        bucketset=BucketSet(
            buckets=[Bucket(key="PHASE2", label="PHASE2", value=100, exactness="exact")],
            total=500,
            unclassified=0,
            semantics="overlapping",
            mode="complete_records",
        ),
    )
    sampled = Panel(
        label="Sampled",
        bucketset=BucketSet(
            buckets=[Bucket(key="PHASE2", label="PHASE2", value=10, exactness="exact")],
            total=9_500,
            unclassified=0,
            semantics="overlapping",
            mode="sampled_then_confirmed",
            sample_size=500,
            sample_coverage=0.053,
        ),
    )

    merged, _ = merge_panels([read_fully, sampled], PHASE)

    assert merged.sample_size == 1_000  # 500 read in full + 500 sampled
    assert merged.sample_coverage == round(1_000 / 10_000, 3)


async def test_coverage_describes_the_chart_that_was_drawn(
    settings: Settings, vocab: Vocabulary
) -> None:
    """Marking a truncated result incomplete was not enough on its own.

    The merged set still held every key, so the note said "showing 10" for a chart drawing 3 and
    bucket_sum totalled bars nobody could see. Coverage now runs on the narrowed set.
    """
    from app.engine.coverage import build_coverage
    from app.render.encode import plotted_axis_keys

    panels = [
        a_panel("A", {f"K{i:02d}": 100 - i for i in range(10)}, total=1_000),
        a_panel("B", {f"K{i:02d}": 50 - i for i in range(10)}, total=500),
    ]
    merged, _ = merge_panels(panels, REGISTRY["lead_sponsor"])

    plotted = merged.plotted_only(plotted_axis_keys(panels, 3))
    coverage, _ = build_coverage(plotted, REGISTRY["lead_sponsor"])

    assert len(plotted.buckets) == 3
    assert plotted.omitted_buckets == 7
    assert coverage.bucket_sum == plotted.bucket_sum  # only what is drawn
    assert "Showing 3 of 10 lead_sponsor values" in (coverage.overlap_note or "")

    untouched = merged.plotted_only(plotted_axis_keys(panels, 20))
    assert untouched.omitted_buckets == 0
    assert untouched.complete


async def test_a_truncated_overlapping_dimension_also_discloses_the_cut(
    settings: Settings, vocab: Vocabulary
) -> None:
    """`complete` was read only in the partition branch.

    So a truncated comparison on phase, condition or country disclosed the cut nowhere in
    meta.coverage — the branch that fires for every multi-valued dimension ignored it.
    """
    from app.engine.coverage import build_coverage
    from app.render.encode import plotted_axis_keys

    panels = [a_panel("A", {f"K{i:02d}": 100 - i for i in range(10)}, total=1_000)]
    merged, _ = merge_panels(panels, PHASE)

    plotted = merged.plotted_only(plotted_axis_keys(panels, 4))
    coverage, _ = build_coverage(plotted, PHASE)

    assert coverage.groupby_semantics == "overlapping"
    assert "Showing 4 of 10 phase values" in (coverage.overlap_note or "")
    assert "multi-valued" in (coverage.overlap_note or "")  # the overlap note is still there
