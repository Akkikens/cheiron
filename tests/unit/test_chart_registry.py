"""Chart selection. SPEC §6.1 — every reachable combination, and the two unreachable types."""

from __future__ import annotations

from itertools import product

from app.engine.bucketset import Bucket, BucketSet
from app.engine.dimensions import QUANTITATIVE_KEYS, REGISTRY
from app.models.plan import (
    AnalysisPlan,
    ChartType,
    GroupBy,
    Intent,
    SeriesSpec,
    StudyFilter,
)
from app.models.request import Options
from app.render.registry import select_chart


def a_plan(**overrides: object) -> AnalysisPlan:
    base: dict[str, object] = {
        "intent": Intent.DISTRIBUTION,
        "filters": StudyFilter(),
        "group_by": GroupBy(dimension="phase"),
        "interpretation": "Distribution of clinical trials across trial phases.",
    }
    return AnalysisPlan(**{**base, **overrides})  # type: ignore[arg-type]


def a_bucketset(
    n: int = 6,
    *,
    mode: str = "server_counts",
    total: int = 100,
    partition: bool | None = None,
) -> BucketSet:
    buckets = [
        Bucket(key=f"K{i}", label=f"Label {i}", value=float(n - i), exactness="exact")
        for i in range(n)
    ]
    semantics = "partition" if (partition if partition is not None else True) else "overlapping"
    return BucketSet(
        buckets=buckets,
        total=total,
        unclassified=0,
        semantics=semantics,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
    )


def test_distribution_under_max_buckets_is_a_bar_chart() -> None:
    chosen, warnings = select_chart(
        a_plan(), a_bucketset(6), REGISTRY["phase"], Options(max_buckets=20)
    )
    assert chosen is ChartType.BAR_CHART
    assert warnings == []


def test_distribution_over_max_buckets_is_a_table() -> None:
    chosen, _ = select_chart(a_plan(), a_bucketset(25), REGISTRY["phase"], Options(max_buckets=20))
    assert chosen is ChartType.TABLE


def test_trend_with_temporal_dim_is_time_series() -> None:
    chosen, _ = select_chart(
        a_plan(intent=Intent.TREND, group_by=GroupBy(dimension="start_year")),
        a_bucketset(10),
        REGISTRY["start_year"],
        Options(),
    )
    assert chosen is ChartType.TIME_SERIES


def test_comparison_with_two_series_is_grouped_bar() -> None:
    plan = a_plan(
        intent=Intent.COMPARISON,
        series=[
            SeriesSpec(label="A", filters=StudyFilter(sponsor="Merck")),
            SeriesSpec(label="B", filters=StudyFilter(sponsor="Pfizer")),
        ],
    )
    chosen, _ = select_chart(plan, a_bucketset(4), REGISTRY["phase"], Options())
    assert chosen is ChartType.GROUPED_BAR_CHART


def test_secondary_on_partition_is_stacked() -> None:
    plan = a_plan(secondary_group_by=GroupBy(dimension="study_type"))
    chosen, _ = select_chart(plan, a_bucketset(4), REGISTRY["study_type"], Options())
    assert chosen is ChartType.STACKED_BAR_CHART


def test_secondary_on_non_partition_is_grouped() -> None:
    plan = a_plan(
        group_by=GroupBy(dimension="phase"),
        secondary_group_by=GroupBy(dimension="study_type"),
    )
    chosen, _ = select_chart(plan, a_bucketset(4), REGISTRY["phase"], Options())
    assert chosen is ChartType.GROUPED_BAR_CHART


def test_geo_is_choropleth() -> None:
    chosen, _ = select_chart(
        a_plan(intent=Intent.GEO, group_by=GroupBy(dimension="country")),
        a_bucketset(5),
        REGISTRY["country"],
        Options(),
    )
    assert chosen is ChartType.CHOROPLETH_MAP


def test_network_in_complete_records_is_network_graph() -> None:
    chosen, warnings = select_chart(
        a_plan(intent=Intent.NETWORK),
        a_bucketset(3, mode="complete_records", total=500),
        REGISTRY["phase"],
        Options(),
    )
    assert chosen is ChartType.NETWORK_GRAPH
    assert warnings == []


def test_network_outside_complete_records_downgrades_and_warns() -> None:
    chosen, warnings = select_chart(
        a_plan(intent=Intent.NETWORK),
        a_bucketset(3, mode="server_counts", total=5_000),
        REGISTRY["phase"],
        Options(),
    )
    assert chosen is ChartType.GROUPED_BAR_CHART
    assert any("complete_records" in warning for warning in warnings)
    assert any("5,000" in warning for warning in warnings)


def test_single_scalar_is_kpi() -> None:
    chosen, _ = select_chart(a_plan(), a_bucketset(1), REGISTRY["phase"], Options())
    assert chosen is ChartType.KPI


def test_list_intent_is_table() -> None:
    chosen, _ = select_chart(
        a_plan(intent=Intent.LIST), a_bucketset(3), REGISTRY["phase"], Options()
    )
    assert chosen is ChartType.TABLE


def test_pie_hint_is_discarded_with_a_warning() -> None:
    """SPEC A7: pie on a non-partition (or anywhere) is discarded, not fatal."""
    plan = AnalysisPlan.model_validate(
        {
            "intent": "distribution",
            "filters": {},
            "group_by": {"dimension": "phase"},
            "interpretation": "Distribution of clinical trials across trial phases.",
            "viz_hint": "pie_chart",
        }
    )
    assert plan.viz_hint is None
    assert plan.discarded_viz_hint == "pie_chart"

    chosen, warnings = select_chart(plan, a_bucketset(6), REGISTRY["phase"], Options())
    assert chosen is ChartType.BAR_CHART
    assert any("pie_chart" in warning for warning in warnings)


def test_stacked_hint_on_overlapping_dimension_is_discarded() -> None:
    plan = a_plan(viz_hint=ChartType.STACKED_BAR_CHART)
    chosen, warnings = select_chart(plan, a_bucketset(6), REGISTRY["phase"], Options())
    assert chosen is ChartType.BAR_CHART
    assert any("stacked_bar_chart" in warning for warning in warnings)


def test_histogram_and_scatter_are_not_returned_for_non_quantitative_intents() -> None:
    """The sweep that used to assert both types were unreachable.

    `enrollment_count` now exists, so they are reachable — but only from their own intents. No
    combination of the other six may produce either type, which is what keeps a distribution
    question from silently answering with a histogram.
    """
    assert QUANTITATIVE_KEYS  # the dimension is present; the sweep below excludes its intents

    intents = [
        Intent.DISTRIBUTION,
        Intent.TREND,
        Intent.COMPARISON,
        Intent.GEO,
        Intent.NETWORK,
        Intent.LIST,
    ]
    cardinalities = [0, 1, 6, 25]
    series_counts = [0, 2]
    partitions = [True, False]
    modes = ["server_counts", "complete_records", "sampled_then_confirmed"]

    seen: set[ChartType] = set()
    for intent, card, n_series, partition, mode in product(
        intents, cardinalities, series_counts, partitions, modes
    ):
        dim = REGISTRY["start_year"] if intent is Intent.TREND else REGISTRY["phase"]
        if intent is Intent.GEO:
            dim = REGISTRY["country"]
        if not dim.partition and partition:
            dim = REGISTRY["study_type"]
        if dim.partition and not partition:
            dim = REGISTRY["phase"]

        series = []
        if n_series >= 2:
            if intent is not Intent.COMPARISON:
                continue
            series = [
                SeriesSpec(label="A", filters=StudyFilter()),
                SeriesSpec(label="B", filters=StudyFilter()),
            ]

        plan = a_plan(
            intent=intent,
            group_by=GroupBy(dimension=dim.key),
            series=series,
        )
        bucketset = a_bucketset(max(card, 1) if card else 0, mode=mode, partition=dim.partition)
        if card == 0:
            bucketset = BucketSet(
                buckets=[],
                total=0,
                unclassified=0,
                semantics="partition" if dim.partition else "overlapping",
                mode=mode,  # type: ignore[arg-type]
            )

        chosen, _ = select_chart(plan, bucketset, dim, Options(max_buckets=20))
        seen.add(chosen)
        assert chosen not in {ChartType.HISTOGRAM, ChartType.SCATTER_PLOT}

    assert ChartType.BAR_CHART in seen
    assert ChartType.TABLE in seen
