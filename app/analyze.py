"""`POST /analyze` orchestration. SPEC §4.4, §5.2, §6.

Planning failure is `unplannable_query`, never `invalid_request`: the request was well-formed;
it is the question that could not be served. T05 records this in `validate_plan`'s docstring;
the route is where it becomes an HTTP response.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.ctg.client import CTGClient, CTGTransport
from app.ctg.vocab import Vocabulary, VocabularyCache
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
from app.engine.modes import counts, network, records
from app.engine.preflight import AggregationModeName, preflight, unimplemented_mode
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan, ChartType, Intent, Metric
from app.models.request import AnalyzeRequest
from app.models.response import (
    AnalyzeResponse,
    Meta,
    Provenance,
    TimingMs,
)
from app.planner.base import PlanResult
from app.planner.heuristic import HeuristicPlanner
from app.planner.validate import enforce_hard_constraints, validate_plan
from app.render.encode import render
from app.render.registry import select_chart


async def analyze(
    request: AnalyzeRequest,
    *,
    transport: CTGTransport,
    vocabulary_cache: VocabularyCache,
    settings: Settings,
) -> AnalyzeResponse:
    client = CTGClient(transport)
    vocab = await vocabulary_cache.get(client)

    request.validate_against(vocab)

    t0 = time.perf_counter()
    plan_result = await _plan(request, vocab)
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
    ctx = new_context(
        client,
        vocab,
        request.options,
        settings=settings,
        data_timestamp=version.data_timestamp,
    )
    ctx.assumptions.extend(assumptions)

    studies: list[dict[str, Any]] | None = None
    try:
        pre = await preflight(plan, dim, ctx, threshold=settings.record_mode_threshold)
        ctx.assumptions.extend(pre.assumptions)

        if pre.total == 0:
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
            bucketset, studies = await _aggregate(plan, dim, ctx, pre.params, pre.total, pre.mode)
    except BudgetExhausted as exc:
        raise budget_error(exc) from exc

    coverage, coverage_warnings = build_coverage(bucketset, dim)
    chart_type, chart_warnings = select_chart(plan, bucketset, dim, request.options)

    if (
        plan.intent is Intent.NETWORK
        and chart_type is ChartType.NETWORK_GRAPH
        and studies is not None
    ):
        visualization, render_warnings = network.build(studies, plan, ctx)
    else:
        visualization, render_warnings = render(plan, bucketset, chart_type, dim, ctx)

    retrieve_ms = int((time.perf_counter() - t1) * 1000)

    warnings = [
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

    return AnalyzeResponse(visualization=visualization, meta=meta)


async def _plan(request: AnalyzeRequest, vocab: Vocabulary) -> PlanResult:
    """Heuristic only until T09. A planning miss is unplannable, not invalid."""
    return await HeuristicPlanner().plan(request, vocab)


async def _aggregate(
    plan: AnalysisPlan,
    dim: Dimension,
    ctx: RunContext,
    params: dict[str, str],
    total: int,
    mode: AggregationModeName,
) -> tuple[BucketSet, list[dict[str, Any]] | None]:
    if mode == "complete_records":
        return await _run_records(plan, dim, ctx, params=params, total=total)

    if mode != "server_counts":
        raise unimplemented_mode(mode, total, dim)

    try:
        bucketset = await counts.run(plan, dim, ctx, params=params, total=total)
    except DataTimestampChanged:
        # SPEC §7: retry the whole group-by once, then fail.
        ctx.data_timestamp = await ctx.observed_data_timestamp()
        try:
            bucketset = await counts.run(plan, dim, ctx, params=params, total=total)
        except DataTimestampChanged as exc:
            raise CheironError(
                ErrorCode.UPSTREAM_ERROR,
                f"ClinicalTrials.gov published a new dataset during the analysis "
                f"({exc.captured} → {exc.observed}) and a retry still saw movement.",
            ) from exc
    return bucketset, None


async def _run_records(
    plan: AnalysisPlan,
    dim: Dimension,
    ctx: RunContext,
    *,
    params: dict[str, str],
    total: int,
) -> tuple[BucketSet, list[dict[str, Any]]]:
    try:
        return await records.run(plan, dim, ctx, params=params, total=total)
    except DataTimestampChanged:
        ctx.data_timestamp = await ctx.observed_data_timestamp()
        try:
            return await records.run(plan, dim, ctx, params=params, total=total)
        except DataTimestampChanged as exc:
            raise CheironError(
                ErrorCode.UPSTREAM_ERROR,
                f"ClinicalTrials.gov published a new dataset during the analysis "
                f"({exc.captured} → {exc.observed}) and a retry still saw movement.",
            ) from exc


def _filters_applied(plan: AnalysisPlan) -> dict[str, Any]:
    dumped = plan.filters.model_dump(exclude_none=True)
    return {key: value for key, value in dumped.items() if value != []}
