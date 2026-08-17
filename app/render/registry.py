"""Chart selection. SPEC §6.1.

A deterministic function of `(intent, cardinality, series count, partition, mode)`. `viz_hint`
breaks ties only and is discarded, with a warning, when it would violate a safety rule.
"""

from __future__ import annotations

from app.engine.bucketset import BucketSet
from app.engine.dimensions import QUANTITATIVE_KEYS, Dimension, is_temporal, resolve
from app.models.plan import AnalysisPlan, ChartType, Intent
from app.models.request import Options
from app.models.response import AggregationMode


def select_chart(
    plan: AnalysisPlan,
    bucketset: BucketSet,
    dim: Dimension,
    options: Options,
) -> tuple[ChartType, list[str]]:
    """Return the chart type and any warnings about discarded hints or safety downgrades."""
    warnings: list[str] = []
    cardinality = len(bucketset.buckets)
    series_count = max(1, len(plan.series))
    chosen = _primary(plan, bucketset, dim, options, cardinality, series_count, warnings)

    if plan.discarded_viz_hint is not None:
        warnings.append(
            f"viz_hint {plan.discarded_viz_hint!r} was discarded because it is not a renderable "
            f"chart type (pie/donut/100%-stacked are forbidden); returning {chosen.value!r}."
        )

    if plan.viz_hint is not None and plan.viz_hint is not chosen:
        if _hint_is_safe(plan.viz_hint, dim, bucketset.mode, cardinality, options, plan):
            if _is_tie(plan.viz_hint, chosen):
                chosen = plan.viz_hint
            else:
                warnings.append(
                    f"viz_hint {plan.viz_hint.value!r} was ignored; the registry selected "
                    f"{chosen.value!r} from the plan shape."
                )
        else:
            warnings.append(
                f"viz_hint {plan.viz_hint.value!r} was discarded because it violates a safety "
                f"rule for this dimension or mode; returning {chosen.value!r}."
            )

    return chosen, warnings


def _primary(
    plan: AnalysisPlan,
    bucketset: BucketSet,
    dim: Dimension,
    options: Options,
    cardinality: int,
    series_count: int,
    warnings: list[str],
) -> ChartType:
    # SPEC §6.1 table, in order. Scatter and histogram became reachable when `enrollment_count`
    # joined the registry; the sweep test asserts they are still never returned for the intents
    # that are not theirs.

    if plan.intent is Intent.TREND and is_temporal(dim):
        return ChartType.TIME_SERIES

    if plan.secondary_group_by is not None:
        # The **secondary** dimension decides, not the primary. Segments within one bar are
        # secondary values, so they sum to their bar only when a study carries exactly one of
        # them. Reading the primary's flag got both cases backwards: phase-by-status is
        # stackable (one status per study) and was grouped, while status-by-phase is not (a
        # study can be in two phases) and would have been stacked, implying a whole that does
        # not exist.
        secondary = resolve(plan.secondary_group_by.dimension)
        if secondary.partition:
            return ChartType.STACKED_BAR_CHART
        return ChartType.GROUPED_BAR_CHART

    if plan.intent is Intent.COMPARISON and 2 <= series_count <= 4:
        return ChartType.GROUPED_BAR_CHART

    if plan.intent is Intent.GEO:
        return ChartType.CHOROPLETH_MAP

    if plan.intent is Intent.NETWORK:
        if bucketset.mode == "complete_records":
            return ChartType.NETWORK_GRAPH
        # SPEC §5.4 / A7: never invent co-occurrence from a relevance-ranked or count-only set.
        warnings.append(
            f"network_graph requires complete_records mode; this result has {bucketset.total:,} "
            f"studies under {bucketset.mode!r}, so returning grouped_bar_chart instead."
        )
        return (
            ChartType.GROUPED_BAR_CHART if cardinality <= options.max_buckets else ChartType.TABLE
        )

    if plan.intent is Intent.SCATTER and QUANTITATIVE_KEYS:
        if bucketset.mode == "complete_records":
            return ChartType.SCATTER_PLOT
        # A scatter plots one point per study, so it needs the records themselves. Above the
        # threshold there are none in memory, and plotting bin midpoints as if they were studies
        # would invent data: the same reasoning that keeps network_graph in record mode.
        warnings.append(
            f"scatter_plot plots one point per study and needs complete_records mode; this "
            f"result has {bucketset.total:,} studies under {bucketset.mode!r}, so returning a "
            f"histogram of the same dimension instead."
        )
        return ChartType.HISTOGRAM

    if plan.intent is Intent.HISTOGRAM and QUANTITATIVE_KEYS:
        return ChartType.HISTOGRAM

    if cardinality == 1 and series_count == 1 and plan.intent is not Intent.LIST:
        return ChartType.KPI

    if plan.intent is Intent.LIST or cardinality > options.max_buckets:
        return ChartType.TABLE

    if plan.intent is Intent.DISTRIBUTION and series_count == 1:
        return ChartType.BAR_CHART

    return ChartType.TABLE


def _hint_is_safe(
    hint: ChartType,
    dim: Dimension,
    mode: AggregationMode,
    cardinality: int,
    options: Options,
    plan: AnalysisPlan,
) -> bool:
    if hint is ChartType.NETWORK_GRAPH and mode != "complete_records":
        return False
    # Pie/donut are not ChartTypes (T04 drops them at parse). Stacking a non-partition implies a
    # false whole.
    #
    # The flag to read is the dimension whose values become the *segments*: the secondary when
    # there is one, the primary otherwise. Reading the primary let a stacked hint through on a
    # status-by-phase cross-tab: segments that sum to more than their bar, which is the exact
    # chart §6.1 calls non-overridable, waved past by the override check itself.
    segment_dim = resolve(plan.secondary_group_by.dimension) if plan.secondary_group_by else dim
    if hint is ChartType.STACKED_BAR_CHART and not segment_dim.partition:
        return False
    return not (hint in (ChartType.SCATTER_PLOT, ChartType.HISTOGRAM) and not QUANTITATIVE_KEYS)


def _is_tie(hint: ChartType, chosen: ChartType) -> bool:
    """A hint breaks a tie when both name the same family of chart."""
    families = [
        frozenset({ChartType.BAR_CHART, ChartType.TABLE, ChartType.KPI}),
        frozenset({ChartType.GROUPED_BAR_CHART, ChartType.STACKED_BAR_CHART}),
        frozenset({ChartType.TIME_SERIES}),
        frozenset({ChartType.CHOROPLETH_MAP}),
    ]
    return any(hint in family and chosen in family for family in families)
