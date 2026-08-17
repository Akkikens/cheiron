"""Two-dimensional results: comparison series and cross-tabs. SPEC §3, §6.1.

Both exist because a chart type can promise a breakdown the data does not contain. Before this
module, `intent=comparison` with two series returned a `grouped_bar_chart` whose every row was
labelled with the *first* series and whose counts came from the base filter — a chart that
asserted a comparison nobody computed. That is precisely the fabrication the service exists to
prevent, so both paths here either compute the breakdown for real or refuse.

**Cost is the whole design constraint.** A comparison costs one full aggregation per series; a
cross-tab costs `primary x secondary` counts. Neither fits an unbounded fan-out inside SPEC §7's
40-request budget, so both check affordability *before* spending and refuse with the arithmetic
shown rather than truncating to whatever fits.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.ctg.essie import Essie
from app.engine.basefilter import with_predicate
from app.engine.bucketset import Bucket, BucketSet
from app.engine.context import RunContext
from app.engine.dimensions import Dimension
from app.engine.modes.counts import _gather_or_fail
from app.engine.modes.records import membership_keys
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan

_MODE_EXACTNESS: dict[str, int] = {
    "sampled_then_confirmed": 0,
    "server_counts": 1,
    "complete_records": 2,
}
"""Least to most exact. A mixed comparison reports the weakest mode it actually used."""

MAX_SERIES = 4
"""SPEC §6.1 selects `grouped_bar_chart` for 2-4 series; beyond that a grouped bar is unreadable
and the fan-out cost is not justifiable."""


@dataclass(frozen=True)
class Panel:
    """One series' aggregation, carrying the label it will be plotted under."""

    label: str
    bucketset: BucketSet


@dataclass(frozen=True)
class CrossCell:
    primary: str
    secondary: str
    value: int


def merge_panels(panels: Sequence[Panel], dim: Dimension) -> tuple[BucketSet, list[str]]:
    """Fold per-series bucket sets into one, for coverage and chart selection.

    `total` is the **sum of the per-series totals**, and that is disclosed rather than glossed:
    a study matching two series is counted in both, so the sum is not a population count. The
    alternative — reporting only the base filter's total — would understate what was actually
    queried, which is worse for a number labelled `total_matching_studies`.
    """
    warnings: list[str] = []
    for panel in panels:
        warnings.extend(panel.bucketset.warnings)

    total = sum(panel.bucketset.total for panel in panels)
    unclassified = sum(panel.bucketset.unclassified for panel in panels)

    if len(panels) > 1:
        warnings.append(
            f"Series totals are summed across {len(panels)} series "
            f"({', '.join(panel.label for panel in panels)}); a study matching more than one "
            f"series is counted once per series, so the total is not a population count."
        )

    merged: dict[str, Bucket] = {}
    for panel in panels:
        for bucket in panel.bucketset.buckets:
            existing = merged.get(bucket.key)
            merged[bucket.key] = (
                bucket
                if existing is None
                else Bucket(
                    key=bucket.key,
                    label=bucket.label,
                    value=existing.value + bucket.value,
                    exactness="exact",
                    citations=existing.citations or bucket.citations,
                    citation_note=existing.citation_note or bucket.citation_note,
                )
            )

    # Each series picks its own mode from its own result size, so they can differ. Reporting
    # the first one's would tell a reader auditing exactness the wrong provenance for half the
    # bars, so a mixed run says so and reports the least exact mode present.
    modes = {panel.bucketset.mode for panel in panels}
    if len(modes) > 1:
        warnings.append(
            "Series used different aggregation modes ("
            + ", ".join(f"{panel.label}: {panel.bucketset.mode}" for panel in panels)
            + "); meta.coverage reports the least exact of them."
        )
    mode = min(modes, key=_MODE_EXACTNESS.__getitem__)

    # A study is "inspected" if its series read every record, or if the sampler saw it. Mixing a
    # 1,000-of-100,000 series with a 900-of-1,000 series and averaging the two ratios claimed
    # 45.5% of the result was inspected when the true figure is 1.9% — a mean of ratios over
    # different populations, disclosed as if it described one.
    inspected = 0
    sampled_any = False
    for panel in panels:
        bucketset = panel.bucketset
        if bucketset.sample_size is not None:
            inspected += bucketset.sample_size
            sampled_any = True
        else:
            # complete_records read every record; server_counts counted every study exactly on
            # the server. Neither is a sample, so both belong in the numerator — counting a
            # 200,000-study server_counts series as uninspected reported 0.2% coverage for a
            # chart whose larger half is exact, which understates exactness as badly as
            # overstating it misleads.
            inspected += bucketset.total

    return (
        BucketSet(
            buckets=list(merged.values()),
            total=total,
            unclassified=unclassified,
            semantics="partition" if dim.partition else "overlapping",
            mode=mode,
            aggregation_capped=any(panel.bucketset.aggregation_capped for panel in panels),
            # One population, one ratio: studies inspected over studies matched.
            sample_size=inspected if sampled_any else None,
            sample_coverage=(round(inspected / total, 3) if sampled_any and total else None),
            warnings=[],
        ),
        warnings,
    )


def too_many_series(count: int) -> CheironError:
    return CheironError(
        ErrorCode.UNPLANNABLE_QUERY,
        f"A comparison of {count} series is more than this service will render; "
        f"{MAX_SERIES} is the maximum.",
        details=[
            {
                "series_requested": count,
                "series_supported": MAX_SERIES,
                "suggestion": "Compare fewer series, or ask one question per series.",
            }
        ],
    )


def crosstab_from_records(
    studies: Sequence[Mapping[str, Any]],
    primary: Dimension,
    secondary: Dimension,
) -> list[CrossCell]:
    """Cross-tab in process, from records already fetched. Free in `complete_records` mode.

    A study contributes to every (primary, secondary) pair it carries, which for two multi-valued
    dimensions is a product — the same overlap `meta.coverage` already discloses for one.
    """
    tally: dict[tuple[str, str], int] = {}
    for study in studies:
        primary_keys = membership_keys(study, primary)
        secondary_keys = membership_keys(study, secondary)
        if not primary_keys or not secondary_keys:
            continue
        for first in primary_keys:
            for second in secondary_keys:
                tally[(first, second)] = tally.get((first, second), 0) + 1

    return [
        CrossCell(primary=first, secondary=second, value=value)
        for (first, second), value in sorted(tally.items(), key=lambda item: -item[1])
    ]


async def crosstab_by_counts(
    plan: AnalysisPlan,
    primary: Dimension,
    secondary: Dimension,
    ctx: RunContext,
    *,
    params: Mapping[str, str],
    primary_keys: Sequence[str],
    secondary_keys: Sequence[str],
) -> list[CrossCell]:
    """One count per cell. Refuses up front when the product does not fit the budget."""
    cells = len(primary_keys) * len(secondary_keys)
    affordable = ctx.upstream_budget - ctx.spent - 1

    if cells > affordable:
        raise unaffordable_crosstab(primary, secondary, cells, affordable, ctx)

    # cells + the post-wave /version recheck below, which counts.run also charges for.
    ctx.spend(cells + 1, f"the {primary.key} by {secondary.key} cross-tab")

    pairs = [(first, second) for first in primary_keys for second in secondary_keys]
    coros = [
        ctx.client.count(
            with_predicate(
                dict(params),
                Essie.and_(
                    Essie.field_eq(primary.area, first), Essie.field_eq(secondary.area, second)
                ),
            )
        )
        for first, second in pairs
    ]

    counts = await _gather_or_fail(coros)
    await ctx.assert_data_unchanged()

    return [
        CrossCell(primary=first, secondary=second, value=count)
        for (first, second), count in zip(pairs, counts, strict=True)
        if count > 0
    ]


def unaffordable_crosstab(
    primary: Dimension,
    secondary: Dimension,
    cells: int,
    affordable: int,
    ctx: RunContext,
) -> CheironError:
    """Refuse with the arithmetic, not with a shrug.

    Truncating the secondary dimension to whatever fits would produce a stacked bar whose
    segments do not add up to their bar, which is a chart that lies about its own total.
    """
    return CheironError(
        ErrorCode.UNPLANNABLE_QUERY,
        f"Grouping {primary.key} by {secondary.key} needs {cells:,} count requests "
        f"({primary.key} x {secondary.key}) but only {affordable} of the "
        f"{ctx.upstream_budget}-request budget remain.",
        details=[
            {
                "cells_required": cells,
                "requests_available": affordable,
                "suggestion": (
                    "Narrow the filters so fewer than the record-mode threshold of studies "
                    "match — the cross-tab is then computed in process at no extra cost — or "
                    "drop secondary_group_by and ask for one dimension at a time."
                ),
            }
        ],
    )


async def run_panels(
    aggregate: Any,
    plan: AnalysisPlan,
    ctx: RunContext,
) -> list[Panel]:
    """Run one aggregation per series, sequentially.

    Sequential on purpose: each series is itself a concurrent fan-out, and running them in
    parallel would multiply peak concurrency against a public NIH service we do not own
    (SPEC §7). The series count is capped at four, so the latency cost is bounded.
    """
    panels: list[Panel] = []
    for spec in plan.series:
        bucketset = await aggregate(spec.filters)
        panels.append(Panel(label=spec.label, bucketset=bucketset))
    return panels


__all__ = [
    "MAX_SERIES",
    "CrossCell",
    "Panel",
    "crosstab_by_counts",
    "crosstab_from_records",
    "merge_panels",
    "run_panels",
    "too_many_series",
    "unaffordable_crosstab",
]
