"""`POST /analyze` orchestration. SPEC §4.4, §5.2, §6.

Planning failure is `unplannable_query`, never `invalid_request`: the request was well-formed;
it is the question that could not be served. T05 records this in `validate_plan`'s docstring;
the route is where it becomes an HTTP response.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.cache import Cache, options_cache_key, result_cache_key
from app.config import Settings
from app.ctg.client import CTGClient, CTGTransport
from app.ctg.vocab import MISSING, Vocabulary, VocabularyCache
from app.engine.bucketset import BucketSet
from app.engine.context import (
    BudgetExhausted,
    DataTimestampChanged,
    RunContext,
    budget_error,
    new_context,
)
from app.engine.coverage import build_coverage
from app.engine.dimensions import Dimension, resolve
from app.engine.modes import counts, network, records, sampled
from app.engine.multi import (
    MAX_SERIES,
    CrossCell,
    Panel,
    crosstab_by_counts,
    crosstab_from_records,
    merge_panels,
    run_panels,
    too_many_series,
)
from app.engine.preflight import (
    AggregationModeName,
    Preflight,
    preflight,
    unimplemented_mode,
)
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan, ChartType, Intent, Metric, StudyFilter
from app.models.request import AnalyzeRequest
from app.models.response import (
    AnalyzeResponse,
    Meta,
    Provenance,
    TimingMs,
)
from app.planner.base import PlanResult
from app.planner.heuristic import HeuristicPlanner
from app.planner.llm import ChatCompleter, LLMPlanner
from app.planner.validate import enforce_hard_constraints, validate_plan
from app.render.encode import (
    plotted_axis_keys,
    plotted_crosstab_keys,
    render,
    render_crosstab,
    render_panels,
    render_scatter,
)
from app.render.registry import select_chart


async def analyze(
    request: AnalyzeRequest,
    *,
    transport: CTGTransport,
    vocabulary_cache: VocabularyCache,
    settings: Settings,
    plan_cache: Cache[AnalysisPlan] | None = None,
    result_cache: Cache[AnalyzeResponse] | None = None,
    completer: ChatCompleter | None = None,
) -> AnalyzeResponse:
    client = CTGClient(transport)
    vocab = await vocabulary_cache.get(client)

    request.validate_against(vocab)

    planner_warnings: list[str] = []
    t0 = time.perf_counter()
    plan_result = await _plan(
        request,
        vocab,
        settings=settings,
        plan_cache=plan_cache,
        completer=completer,
        warnings=planner_warnings,
    )
    plan, assumptions = enforce_hard_constraints(plan_result.plan, request)
    plan_ms = int((time.perf_counter() - t0) * 1000)

    errors = validate_plan(plan, vocab)
    if errors:
        raise CheironError(
            ErrorCode.UNPLANNABLE_QUERY,
            "This question cannot be answered from clinical trial metadata as planned.",
            details=[{"message": message} for message in errors],
        )

    dim = resolve(plan.group_by.dimension)

    t1 = time.perf_counter()
    version = await client.version()

    # SPEC §7: keyed on the plan *and* the dataset revision, so an entry cannot outlive the data
    # it describes. `retrieved_at` on a hit is deliberately the original retrieval time — these
    # numbers were fetched then, and restamping them with "now" would overstate their freshness.
    cache_key = result_cache_key(
        plan.normalized_key(), version.data_timestamp, options_cache_key(request.options)
    )
    if result_cache is not None:
        hit = result_cache.get(cache_key)
        if hit is not None:
            return hit

    ctx = new_context(
        client,
        vocab,
        request.options,
        settings=settings,
        data_timestamp=version.data_timestamp,
    )
    ctx.assumptions.extend(assumptions)

    studies: list[dict[str, Any]] | None = None
    panels: list[Panel] | None = None
    cells: list[CrossCell] | None = None
    try:
        if len(plan.series) > MAX_SERIES:
            raise too_many_series(len(plan.series))

        if plan.metric is not Metric.STUDY_COUNT and len(plan.series) > 1:
            # The single-series path guards this after preflight; a comparison has one preflight
            # per series, so without a guard here the counts fan-out returns *study counts* that
            # are then labelled "Total enrollment" with unit "participants". Refuse up front:
            # a comparison of enrollment needs every series inside record mode, which cannot be
            # known before spending N preflights.
            raise records.enrollment_comparison_unplannable(settings.record_mode_threshold)

        if len(plan.series) > 1:
            # A comparison is N independent analyses. Each series gets its own preflight, mode
            # selection, and fan-out, because each has its own filters and therefore its own
            # result size — one series can be small enough for record mode while another is not.
            panels = await _run_series(plan, dim, ctx, settings=settings)
            bucketset, merge_warnings = merge_panels(panels, dim)
            ctx.warnings.extend(merge_warnings)
            pre = None
        else:
            pre = await preflight(plan, dim, ctx, threshold=settings.record_mode_threshold)
            ctx.assumptions.extend(pre.assumptions)

        if pre is None:
            pass
        elif pre.total == 0:
            # A4: empty result, never a fabricated row. Skip mode selection entirely — a zero
            # total would otherwise pick complete_records and page an empty set.
            bucketset = BucketSet(
                buckets=[],
                total=0,
                unclassified=0,
                semantics="partition" if dim.partition else "overlapping",
                mode="server_counts",
            )
        else:
            if plan.metric is not Metric.STUDY_COUNT and pre.mode != "complete_records":
                raise records.enrollment_unplannable(pre.total, settings.record_mode_threshold)
            bucketset, studies = await _aggregate(
                plan,
                dim,
                ctx,
                pre.params,
                pre.total,
                pre.mode,
                sample_pages=settings.sample_pages,
            )

            if plan.secondary_group_by is not None:
                cells = await _crosstab(
                    plan, dim, ctx, pre=pre, bucketset=bucketset, studies=studies
                )
    except BudgetExhausted as exc:
        raise budget_error(exc) from exc

    # A comparison or cross-tab caps its shared axis, so the bucket set holds categories the
    # chart will not draw. Coverage is built from that set, so narrow it first: otherwise
    # bucket_sum totals bars nobody can see and the note counts categories nobody was shown.
    if panels is not None:
        bucketset = bucketset.plotted_only(plotted_axis_keys(panels, request.options.max_buckets))
    elif cells:
        # `elif cells:` and not `is not None`: an empty cross-tab means no study had *both*
        # dimensions, which is a data fact, not a cap. Narrowing to an empty key set emptied the
        # bucket set and reported "Showing 0 of 5 values, the rest cut by options.max_buckets" —
        # naming the wrong cause for a chart that has nothing to draw.
        bucketset = bucketset.plotted_only(
            plotted_crosstab_keys(cells, request.options.max_buckets)
        )
    elif cells is not None:
        ctx.warnings.append(
            f"No study matched both {dim.key} and "
            f"{plan.secondary_group_by.dimension if plan.secondary_group_by else 'the secondary'}"
            f", so the cross-tab is empty; the breakdown is absent from the data, not truncated."
        )

    coverage, coverage_warnings = build_coverage(
        bucketset, dim, counts_studies=plan.metric is Metric.STUDY_COUNT
    )
    chart_type, chart_warnings = select_chart(plan, bucketset, dim, request.options)

    if (
        plan.intent is Intent.NETWORK
        and chart_type is ChartType.NETWORK_GRAPH
        and studies is not None
    ):
        visualization, render_warnings = network.build(studies, plan, ctx)
    elif chart_type is ChartType.SCATTER_PLOT and studies is not None:
        visualization, render_warnings = render_scatter(plan, studies, bucketset, dim, ctx)
    elif panels is not None:
        visualization, render_warnings = render_panels(plan, panels, bucketset, dim, ctx)
    elif cells is not None and plan.secondary_group_by is not None:
        visualization, render_warnings = render_crosstab(
            plan,
            cells,
            bucketset,
            dim,
            resolve(plan.secondary_group_by.dimension),
            ctx,
            chart_type,
        )
    else:
        visualization, render_warnings = render(plan, bucketset, chart_type, dim, ctx)

    retrieve_ms = int((time.perf_counter() - t1) * 1000)

    warnings = [
        *planner_warnings,
        *ctx.warnings,
        *bucketset.warnings,
        *coverage_warnings,
        *chart_warnings,
        *render_warnings,
    ]

    retrieved_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_ms = plan_ms + retrieve_ms

    meta = Meta(
        interpretation=plan.interpretation,
        planner=plan_result.planner,
        filters_applied=_filters_applied(plan),
        assumptions=list(ctx.assumptions),
        warnings=warnings,
        total_matching_studies=bucketset.total,
        coverage=coverage,
        provenance=Provenance(
            api_version=version.api_version,
            data_timestamp=version.data_timestamp,
            retrieved_at=retrieved_at,
        ),
        timing_ms=TimingMs(plan=plan_ms, retrieve=retrieve_ms, total=total_ms),
        api_query_log=list(client.query_log) if request.options.explain else None,
        plan=plan if request.options.explain else None,
    )

    response = AnalyzeResponse(visualization=visualization, meta=meta)
    if result_cache is not None:
        result_cache.set(cache_key, response)
    return response


async def _plan(
    request: AnalyzeRequest,
    vocab: Vocabulary,
    *,
    settings: Settings,
    plan_cache: Cache[AnalysisPlan] | None,
    completer: ChatCompleter | None,
    warnings: list[str],
) -> PlanResult:
    """LLM when enabled, heuristic otherwise. A planning miss is unplannable, not invalid.

    With `LLM_ENABLED=false` the planner is never constructed, so the OpenAI SDK is never
    imported and no key is read — SPEC A6's degraded mode is an absence of code, not a branch
    inside it.
    """
    if not settings.llm_enabled or completer is None:
        return await HeuristicPlanner().plan(request, vocab)

    return await LLMPlanner(completer, cache=plan_cache, warnings=warnings).plan(request, vocab)


async def _run_series(
    plan: AnalysisPlan,
    dim: Dimension,
    ctx: RunContext,
    *,
    settings: Settings,
) -> list[Panel]:
    """One complete analysis per series, each with its own preflight and mode."""

    async def aggregate_one(filters: StudyFilter) -> BucketSet:
        series_plan = plan.model_copy(update={"filters": filters, "series": []})
        pre = await preflight(series_plan, dim, ctx, threshold=settings.record_mode_threshold)
        if pre.total == 0:
            return BucketSet(
                buckets=[],
                total=0,
                unclassified=0,
                semantics="partition" if dim.partition else "overlapping",
                mode="server_counts",
            )
        bucketset, _ = await _aggregate(
            series_plan,
            dim,
            ctx,
            pre.params,
            pre.total,
            pre.mode,
            sample_pages=settings.sample_pages,
        )
        return bucketset

    return await run_panels(aggregate_one, plan, ctx)


async def _crosstab(
    plan: AnalysisPlan,
    dim: Dimension,
    ctx: RunContext,
    *,
    pre: Preflight,
    bucketset: BucketSet,
    studies: list[dict[str, Any]] | None,
) -> list[CrossCell]:
    """Compute the secondary breakdown, free from records or one count per cell.

    In `complete_records` mode the studies are already in memory, so the cross-tab costs nothing;
    everywhere else it is a product of two bucket lists and is refused when it does not fit the
    budget, rather than truncated into a chart whose segments do not add up.
    """
    assert plan.secondary_group_by is not None
    secondary = resolve(plan.secondary_group_by.dimension)

    if studies is not None:
        return crosstab_from_records(studies, dim, secondary)

    if secondary.enum_name is None or dim.enum_name is None:
        raise CheironError(
            ErrorCode.UNPLANNABLE_QUERY,
            f"A {dim.key} by {secondary.key} breakdown needs both dimensions to have closed "
            f"vocabularies at this result size; open-vocabulary labels would have to be sampled, "
            f"and a sampled cross-tab cannot be confirmed cell by cell within the budget.",
            details=[
                {
                    "suggestion": (
                        "Narrow the filters so the record-mode threshold applies, or drop "
                        "secondary_group_by."
                    )
                }
            ],
        )

    primary_keys = [key for key in ctx.vocab.sort_order(dim.enum_name) if key != MISSING]
    secondary_keys = [key for key in ctx.vocab.sort_order(secondary.enum_name) if key != MISSING]

    return await crosstab_by_counts(
        plan,
        dim,
        secondary,
        ctx,
        params=pre.params,
        primary_keys=[key for key in primary_keys if key in set(ctx.vocab.values(dim.enum_name))],
        secondary_keys=[
            key for key in secondary_keys if key in set(ctx.vocab.values(secondary.enum_name))
        ],
    )


async def _aggregate(
    plan: AnalysisPlan,
    dim: Dimension,
    ctx: RunContext,
    params: dict[str, str],
    total: int,
    mode: AggregationModeName,
    *,
    sample_pages: int,
) -> tuple[BucketSet, list[dict[str, Any]] | None]:
    if mode == "complete_records":
        return await _once_more_on_new_data(
            ctx, lambda: records.run(plan, dim, ctx, params=params, total=total)
        )

    if mode == "sampled_then_confirmed":
        bucketset = await _once_more_on_new_data(
            ctx,
            lambda: sampled.run(
                plan, dim, ctx, params=params, total=total, sample_pages=sample_pages
            ),
        )
        return bucketset, None

    if mode != "server_counts":
        raise unimplemented_mode(mode, total, dim)

    bucketset = await _once_more_on_new_data(
        ctx, lambda: counts.run(plan, dim, ctx, params=params, total=total)
    )
    return bucketset, None


async def _once_more_on_new_data[T](ctx: RunContext, run: Callable[[], Awaitable[T]]) -> T:
    """SPEC §7: if the dataset moved mid-fan-out, redo the whole group-by once, then fail.

    Retrying the *whole* group-by rather than the failed bucket is the point — the guarantee is
    that no chart ever mixes two dataset revisions, and a per-bucket retry would produce exactly
    that mixture.

    The re-capture between attempts is load-bearing: the retry runs against the dataset that
    *replaced* the one we started on, so without it the second attempt compares against a stale
    timestamp and fails by construction.
    """
    try:
        return await run()
    except DataTimestampChanged:
        ctx.data_timestamp = await ctx.observed_data_timestamp()

    try:
        return await run()
    except DataTimestampChanged as exc:
        raise CheironError(
            ErrorCode.UPSTREAM_ERROR,
            f"ClinicalTrials.gov published a new dataset during the analysis "
            f"({exc.captured} → {exc.observed}) and a retry still saw movement.",
        ) from exc


def _filters_applied(plan: AnalysisPlan) -> dict[str, Any]:
    dumped = plan.filters.model_dump(exclude_none=True)
    return {key: value for key, value in dumped.items() if value != []}
