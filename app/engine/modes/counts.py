"""`server_counts`: one count request per bucket, issued concurrently. SPEC §5.2, §5.3.

**The failure semantics here are inverted from the rest of the codebase.** Everywhere else a
degraded answer beats no answer; here a single failed bucket must kill the whole group-by,
because SPEC §4.5 forbids rendering a partial aggregation. A bar chart missing one bar does not
look broken — it looks like a finding. So `gather(return_exceptions=True)` collects every
outcome, the remaining tasks are cancelled, and the first exception is re-raised.

Citation fetches ride the same wave but with the opposite failure rule: a missing citation is
missing evidence for a number that is still exact, so that bucket keeps its count and loses only
its citations (see `citations.py`).

Counts from this mode are `exactness="exact"` regardless of how large the result set is: each
bucket is a `countTotal` on a predicate, not a sample.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from datetime import date
from typing import Any, Final

from app.ctg.essie import Essie
from app.engine.basefilter import with_predicate, year_span
from app.engine.bucketset import Bucket, BucketSet
from app.engine.citations import (
    ORDERING_ASSUMPTION,
    citation_note,
    plan_citation_budget,
    sample_citations,
)
from app.engine.context import RunContext
from app.engine.dimensions import Dimension, is_temporal
from app.models.plan import AnalysisPlan
from app.models.response import AggregationMode, Citation

MODE_NAME: Final[AggregationMode] = "server_counts"

DEFAULT_TREND_YEARS = 10
"""How far back an unbounded trend reaches. Notes §6.3 found start dates from 1900 to 2099, so
an unbounded range would spend most of its buckets on garbage at both ends."""


async def run(
    plan: AnalysisPlan,
    dim: Dimension,
    ctx: RunContext,
    *,
    params: dict[str, str],
    total: int,
) -> BucketSet:
    """Count every bucket of `dim` under `params`, plus one for the absent-field case."""
    keys = _bucket_keys(plan, dim, ctx)
    warnings: list[str] = []

    max_buckets = ctx.options.max_buckets
    if len(keys) > max_buckets:
        warnings.append(
            f"{dim.key} has {len(keys)} buckets; showing the first {max_buckets} because "
            f"options.max_buckets is {max_buckets}."
        )
        keys = keys[:max_buckets]

    # Counts: one per bucket, +1 for MISSING, +1 for the post-wave timestamp recheck.
    count_cost = len(keys) + 2
    cite_fetches, cite_warnings = plan_citation_budget(
        ctx, bucket_count=len(keys), count_cost=count_cost
    )
    warnings.extend(cite_warnings)

    ctx.spend(count_cost + cite_fetches, f"the {dim.key} count fan-out")

    predicates = [(key, _predicate(dim, key, plan)) for key in keys]
    per_datum = ctx.options.citations_per_datum if cite_fetches else 0

    # Citation coroutines are scheduled alongside the counts so they share the wave, not a
    # second round trip after it. Only the first `cite_fetches` buckets get a fetch; the rest
    # are the ones cut when the budget was tight.
    cite_coros: list[Coroutine[Any, Any, tuple[list[Citation], str]]] = []
    for index, (_, predicate) in enumerate(predicates):
        if index < cite_fetches:
            cite_coros.append(
                sample_citations(
                    predicate,
                    dim,
                    per_datum,
                    ctx,
                    # Contributing is unknown until counts land; size the page to `n` and
                    # rebuild the note with the real total below.
                    contributing=per_datum,
                    base_params=params,
                )
            )

    count_coros: list[Coroutine[Any, Any, int]] = [
        ctx.client.count(with_predicate(params, predicate)) for _, predicate in predicates
    ] + [ctx.client.count(with_predicate(params, Essie.missing(dim.area)))]

    # Counts fail-all; citations fail soft. Run them as two gathers in parallel so a citation
    # exception cannot cancel a count, and a count exception still cancels everything.
    counts_task = asyncio.create_task(_gather_or_fail(count_coros))
    cites_task = asyncio.create_task(_gather_citations(cite_coros, keys[:cite_fetches], warnings))
    counts, citation_results = await asyncio.gather(counts_task, cites_task)

    *bucket_counts, unclassified = counts

    await ctx.assert_data_unchanged()

    if cite_fetches:
        ctx.assumptions.append(ORDERING_ASSUMPTION)

    buckets: list[Bucket] = []
    for index, ((key, _), count) in enumerate(
        zip(predicates, bucket_counts, strict=True),
    ):
        citations: list[Citation] = []
        note: str | None = None
        if index < len(citation_results) and count > 0:
            raw_citations, _ = citation_results[index]
            citations = raw_citations[: min(per_datum, int(count))]
            note = citation_note(len(citations), int(count)) if citations else None
        elif index < len(citation_results) and count == 0:
            citations, note = [], None

        buckets.append(
            Bucket(
                key=key,
                label=_label(dim, key, ctx),
                value=count,
                exactness="exact",
                citations=citations,
                citation_note=note,
            )
        )

    return BucketSet(
        buckets=buckets,
        total=total,
        unclassified=unclassified,
        semantics="partition" if dim.partition else "overlapping",
        mode=MODE_NAME,
        warnings=warnings,
    )


async def _gather_or_fail(counts: Sequence[Coroutine[Any, Any, int]]) -> list[int]:
    """Run every count concurrently; if any fails, cancel the rest and raise the first failure.

    `return_exceptions=True` is what makes the cancellation orderly: without it, `gather`
    propagates as soon as one task fails and the sibling requests keep running against upstream
    with nobody waiting for them.
    """
    tasks = [asyncio.ensure_future(count) for count in counts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise failures[0]

    return [result for result in results if not isinstance(result, BaseException)]


async def _gather_citations(
    coros: Sequence[Coroutine[Any, Any, tuple[list[Citation], str]]],
    keys: Sequence[str],
    warnings: list[str],
) -> list[tuple[list[Citation], str]]:
    """Citation failure is non-fatal: drop that bucket's citations and warn."""
    if not coros:
        return []

    tasks = [asyncio.ensure_future(coro) for coro in coros]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[tuple[list[Citation], str]] = []
    for key, result in zip(keys, results, strict=True):
        if isinstance(result, BaseException):
            warnings.append(
                f"Citations for bucket {key!r} could not be retrieved ({type(result).__name__}); "
                f"the count is still exact."
            )
            out.append(([], ""))
        else:
            out.append(result)
    return out


def _bucket_keys(plan: AnalysisPlan, dim: Dimension, ctx: RunContext) -> list[str]:
    if is_temporal(dim):
        return [str(year) for year in _years(plan)]

    if dim.enum_name is None:
        raise ValueError(
            f"{dim.key!r} has an open vocabulary; server_counts needs a closed one "
            "(sampled_then_confirmed handles the rest)"
        )

    # Never hardcoded: the live enum is the source of *which* buckets exist, and `sort_order`
    # only decides the order they come back in.
    #
    # The intersection is the load-bearing part. `sort_order` is SPEC §4's display array, so it
    # legitimately contains the synthetic MISSING bucket, which is not an upstream enum value.
    # Enumerating it directly produced `AREA[Phase]\MISSING` — the escaped literal word — which
    # counts zero at HTTP 200 and duplicates the unclassified probe.
    live = set(ctx.vocab.values(dim.enum_name))
    return [value for value in ctx.vocab.sort_order(dim.enum_name) if value in live]


def _years(plan: AnalysisPlan) -> list[int]:
    step = plan.group_by.bin.size if plan.group_by.bin else 1

    span = year_span(plan.filters)
    if span is None:
        latest = date.today().year
        return list(range(latest - DEFAULT_TREND_YEARS + 1, latest + 1, step))

    first, last = span
    return list(range(first.year, last.year + 1, step))


def _predicate(dim: Dimension, key: str, plan: AnalysisPlan) -> str:
    if not is_temporal(dim):
        return Essie.field_eq(dim.area, key)

    step = plan.group_by.bin.size if plan.group_by.bin else 1
    start = int(key)
    return Essie.date_range(dim.area, date(start, 1, 1), date(start + step - 1, 12, 31))


def _label(dim: Dimension, key: str, ctx: RunContext) -> str:
    if dim.enum_name is None:
        return key
    return ctx.vocab.label(dim.enum_name, key)
