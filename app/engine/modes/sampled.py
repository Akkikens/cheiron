"""`sampled_then_confirmed`: discover labels from a sample, confirm each against the corpus.

SPEC §5.2, §4.3, A2, A3. The honest handling of an open vocabulary at scale: **every displayed
count is exact; the label set may be incomplete; and the incompleteness is disclosed in numbers.**

Three phases:

1. **Discover.** Page the base filter with a projection over the membership field and tally raw
   label frequencies. These are candidates, *not* counts. There is no default sort upstream
   (notes §3), so the sample is relevance-ranked and its frequencies are biased — reporting them
   as values would be exactly the fabrication this service exists to avoid.
2. **Confirm.** One count-only request per candidate, concurrently, through
   `COVERAGE[FullMatch]`. Without it `AREA[LeadSponsorName]"Merck"` returns 2,733 against the
   correct 1,841 (notes §2, SPEC A2) — a short free-text name is a substring of longer ones.
   T06 measured that `FullMatch` is a no-op on `LocationCountry`, whose values are normalized
   exact terms; that is a fact about one field, not a licence to drop the operator, and T03
   measured over-scoping as harmless. So it is applied uniformly, with no per-dimension opt-out.
3. **Disclose.** `sample_size`, `sample_coverage`, and a warning saying which half of the result
   is exact and which half might be missing. A bare `truncated: true` is forbidden (SPEC §4.3).

Confirmation counts fail-all like `server_counts`: a bar missing from a sponsor chart reads as a
finding, not as an outage. Citations fail soft, as everywhere else.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Coroutine, Mapping
from typing import Any, Final

from app.ctg.essie import Essie
from app.engine.basefilter import with_predicate
from app.engine.bucketset import Bucket, BucketSet
from app.engine.citations import (
    ORDERING_ASSUMPTION,
    citation_note,
    fields_projection,
    plan_citation_budget,
    sample_citations,
)
from app.engine.context import RunContext
from app.engine.dimensions import Dimension
from app.engine.modes.counts import _gather_citations, _gather_or_fail
from app.engine.modes.records import membership_keys
from app.models.plan import AnalysisPlan
from app.models.response import AggregationMode, Citation

MODE_NAME: Final[AggregationMode] = "sampled_then_confirmed"
PAGE_SIZE: Final = 1000
"""Never above 1,000: upstream clamps silently (notes §3), so a larger value would quietly
sample less than the disclosure claims."""

COVERAGE_CAVEAT: Final = (
    "Label counts were confirmed with COVERAGE[FullMatch]. ClinicalTrials.gov documents that "
    "COVERAGE and EXPANSION are not fully implemented on the modernized site, so exact-match "
    "confirmation is treated as exact but may be approximate."
)

FREE_TEXT_CAVEAT: Final = (
    "Lead sponsor names are free text — the corpus holds 51,610 distinct values, and "
    "'Novartis' and 'Novartis Pharmaceuticals' are separate labels that are NOT merged here. "
    "Group by sponsor_class for a clean partition."
)


async def run(
    plan: AnalysisPlan,
    dim: Dimension,
    ctx: RunContext,
    *,
    params: dict[str, str],
    total: int,
    sample_pages: int,
) -> BucketSet:
    """Discover labels from a sample, confirm each exactly, and disclose the gap."""
    warnings: list[str] = []

    frequencies, sample_size = await _discover(ctx, params, dim, sample_pages)
    if not frequencies:
        warnings.append(
            f"No {dim.key} values were found in the {sample_size:,}-study sample, so no labels "
            f"could be confirmed. The {total:,} matching studies may all lack this field."
        )
        return _empty(total, sample_size, dim, warnings)

    requested_k = ctx.options.max_buckets
    candidates, k_warning = _candidates(frequencies, requested_k, ctx)
    if k_warning:
        warnings.append(k_warning)

    # Confirmations + the MISSING probe + the post-wave timestamp recheck.
    count_cost = len(candidates) + 2
    cite_fetches, cite_warnings = plan_citation_budget(
        ctx, bucket_count=len(candidates), count_cost=count_cost
    )
    warnings.extend(cite_warnings)
    ctx.spend(count_cost + cite_fetches, f"confirming {len(candidates)} {dim.key} labels")

    predicates = [(label, Essie.full_match(dim.area, label)) for label in candidates]
    per_datum = ctx.options.citations_per_datum if cite_fetches else 0

    cite_coros: list[Coroutine[Any, Any, tuple[list[Citation], str]]] = [
        sample_citations(
            predicate,
            dim,
            per_datum,
            ctx,
            contributing=per_datum,
            base_params=params,
        )
        for index, (_, predicate) in enumerate(predicates)
        if index < cite_fetches
    ]

    count_coros: list[Coroutine[Any, Any, int]] = [
        ctx.client.count(with_predicate(params, predicate)) for _, predicate in predicates
    ] + [ctx.client.count(with_predicate(params, Essie.missing(dim.area)))]

    counts_task = asyncio.create_task(_gather_or_fail(count_coros))
    cites_task = asyncio.create_task(
        _gather_citations(cite_coros, candidates[:cite_fetches], warnings)
    )
    counts, citation_results = await asyncio.gather(counts_task, cites_task)

    *confirmed, unclassified = counts

    await ctx.assert_data_unchanged()

    if cite_fetches:
        ctx.assumptions.append(ORDERING_ASSUMPTION)
    ctx.assumptions.append(COVERAGE_CAVEAT)
    if dim.key == "lead_sponsor":
        ctx.assumptions.append(FREE_TEXT_CAVEAT)

    buckets, dropped = _buckets(predicates, confirmed, citation_results, per_datum, dim)
    if dropped:
        # A label the sample saw but the corpus cannot confirm is either a sampling artifact or
        # an escaping bug. Hiding it silently would mask the second one.
        warnings.append(
            f"{len(dropped)} discovered label(s) confirmed to zero studies and were dropped: "
            f"{', '.join(repr(label) for label in dropped[:5])}"
            f"{'…' if len(dropped) > 5 else ''}. An exact-match count of zero for a label seen "
            f"in the sample indicates a sampling artifact or an escaping fault, not an empty "
            f"bucket."
        )

    coverage = round(sample_size / total, 3) if total else None
    warnings.append(_disclosure(sample_size, total))

    return BucketSet(
        buckets=buckets,
        total=total,
        unclassified=unclassified,
        semantics="partition" if dim.partition else "overlapping",
        mode=MODE_NAME,
        # Top-K by construction: complete only when every label the sample found was confirmed.
        aggregation_capped=len(buckets) + len(dropped) < len(frequencies),
        sample_size=sample_size,
        sample_coverage=coverage,
        warnings=warnings,
    )


async def _discover(
    ctx: RunContext,
    params: Mapping[str, str],
    dim: Dimension,
    sample_pages: int,
) -> tuple[Counter[str], int]:
    """Phase 1. Returns raw label frequencies and how many studies were actually examined.

    The frequencies decide *which* labels to confirm and never *how many* studies carry them.
    """
    page_params = dict(params)
    page_params["pageSize"] = str(PAGE_SIZE)
    page_params["fields"] = fields_projection(dim)
    page_params.pop("countTotal", None)

    frequencies: Counter[str] = Counter()
    examined = 0
    token: str | None = None

    for page_number in range(sample_pages):
        ctx.spend(1, f"label-discovery page {page_number + 1}")
        page = await ctx.client.page(page_params, page_token=token)
        for study in page.studies:
            examined += 1
            keys = membership_keys(study, dim)
            if keys:
                frequencies.update(keys)
        if page.next_page_token is None:
            break
        token = page.next_page_token

    return frequencies, examined


def _candidates(
    frequencies: Counter[str], requested_k: int, ctx: RunContext
) -> tuple[list[str], str | None]:
    """Top-K candidates by sample frequency, reduced if the budget cannot afford K.

    Reserves two requests beyond the confirmations (the MISSING probe and the timestamp
    recheck), so the reduction happens here rather than as a mid-wave budget failure.
    """
    ranked = [label for label, _ in frequencies.most_common()]
    affordable = ctx.upstream_budget - ctx.spent - 2
    k = min(requested_k, len(ranked), max(affordable, 0))

    warning: str | None = None
    if k < min(requested_k, len(ranked)):
        warning = (
            f"Confirmed the top {k} of {requested_k} requested labels; the upstream request "
            f"budget ({ctx.upstream_budget}) had room for no more. Labels ranked below {k} in "
            f"the sample are absent from this chart."
        )
    return ranked[:k], warning


def _buckets(
    predicates: list[tuple[str, str]],
    confirmed: list[int],
    citation_results: list[tuple[list[Citation], str]],
    per_datum: int,
    dim: Dimension,
) -> tuple[list[Bucket], list[str]]:
    """Confirmed counts become buckets; zero-confirmed labels are dropped and named."""
    buckets: list[Bucket] = []
    dropped: list[str] = []

    for index, ((label, _), count) in enumerate(zip(predicates, confirmed, strict=True)):
        if count <= 0:
            dropped.append(label)
            continue

        citations: list[Citation] = []
        note: str | None = None
        if index < len(citation_results):
            raw_citations, _ = citation_results[index]
            citations = raw_citations[: min(per_datum, count)]
            note = citation_note(len(citations), count) if citations else None

        buckets.append(
            Bucket(
                key=label,
                # Open vocabularies have no enum to translate through: the upstream string is
                # the label, verbatim.
                label=label,
                value=count,
                # Each count is a countTotal on an exact-match predicate. The sampling is in
                # *which* labels are here, never in the numbers beside them.
                exactness="exact",
                citations=citations,
                citation_note=note,
            )
        )

    buckets.sort(key=lambda bucket: (-bucket.value, bucket.key))
    return buckets, dropped


def _disclosure(sample_size: int, total: int) -> str:
    """SPEC §4.3's required sentence. Per-label counts exact; the label *set* may be incomplete.

    When the sample reached every matching study there is no "outside the sample", and saying a
    label might be hiding there would be a disclaimer rather than a disclosure — the kind of
    hedging that teaches a reader to ignore the warnings that do matter.
    """
    percent = (sample_size / total * 100) if total else 0.0
    if total and sample_size >= total:
        return (
            f"Labels were discovered from all {total:,} matching studies, so the label set is "
            f"complete. Each displayed count is exact and confirmed against the full corpus."
        )
    return (
        f"Labels were discovered from a {sample_size:,}-study sample ({percent:.1f}% of "
        f"{total:,} matching studies). Each displayed count is exact and confirmed against the "
        f"full corpus; however, a label appearing only outside the sample may be missing from "
        f"this chart."
    )


def _empty(total: int, sample_size: int, dim: Dimension, warnings: list[str]) -> BucketSet:
    return BucketSet(
        buckets=[],
        total=total,
        unclassified=total,
        semantics="partition" if dim.partition else "overlapping",
        mode=MODE_NAME,
        sample_size=sample_size,
        sample_coverage=round(sample_size / total, 3) if total else None,
        warnings=warnings,
    )
