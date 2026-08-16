"""`server_counts`: one count request per bucket, issued concurrently. SPEC §5.2, §5.3.

**The failure semantics here are inverted from the rest of the codebase.** Everywhere else a
degraded answer beats no answer; here a single failed bucket must kill the whole group-by,
because SPEC §4.5 forbids rendering a partial aggregation. A bar chart missing one bar does not
look broken — it looks like a finding. So `gather(return_exceptions=True)` collects every
outcome, the remaining tasks are cancelled, and the first exception is re-raised.

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
from app.engine.context import RunContext
from app.engine.dimensions import Dimension, is_temporal
from app.models.plan import AnalysisPlan
from app.models.response import AggregationMode

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

    # One per bucket, +1 for the MISSING probe every mode issues (SPEC §5.2), +1 for the
    # post-wave timestamp recheck. The recheck is charged because it is a real conditional
    # request; leaving it out would make the budget understate upstream traffic by one per
    # group-by, which is exactly the kind of quiet drift the budget exists to prevent.
    ctx.spend(len(keys) + 2, f"the {dim.key} count fan-out")

    predicates = [(key, _predicate(dim, key, plan)) for key in keys]
    counts = await _gather_or_fail(
        [ctx.client.count(with_predicate(params, predicate)) for _, predicate in predicates]
        + [ctx.client.count(with_predicate(params, Essie.missing(dim.area)))]
    )

    *bucket_counts, unclassified = counts

    # SPEC §7: a mid-run dataset change means two revisions in one chart. The caller retries the
    # whole group-by once. This has to be a live read, or the guarantee is decorative.
    await ctx.assert_data_unchanged()

    return BucketSet(
        buckets=[
            Bucket(
                key=key,
                label=_label(dim, key, ctx),
                value=count,
                exactness="exact",
            )
            for (key, _), count in zip(predicates, bucket_counts, strict=True)
        ],
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
