"""`complete_records`: page every matching study and aggregate in-process. SPEC §5.2.

This is where the system is at its best: exact, unbiased, every dimension available, citations
free from the in-memory page. Pages are strictly serial (notes §1): a `pageToken` is bound to
the exact parameter set that produced it, so there is no parallel fetch and no offset arithmetic.

**Why 2,000.** `pageSize` is silently clamped at 1,000, so 2k studies is two round trips (~1 s).
50k would be ~50 serial round trips and 25-50 s: not a request-path number. That is why the
threshold is a property of upstream paging, not a tuning knob.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.engine.bucketset import Bucket, BucketSet
from app.engine.citations import (
    ORDERING_ASSUMPTION,
    citations_from_records,
    nct_id_of,
    value_at,
)
from app.engine.context import RunContext
from app.engine.dimensions import QUANTITATIVE_KEYS, REGISTRY, Dimension, bin_key, is_temporal
from app.models.plan import AnalysisPlan, Metric
from app.models.response import AggregationMode, Citation

MODE_NAME: Final[AggregationMode] = "complete_records"
PAGE_SIZE: Final = 1000
ENROLLMENT_PATH: Final = "protocolSection.designModule.enrollmentInfo.count"


def fields_projection() -> str:
    """One projection covering every dimension's membership field plus enrollment and NCTId.

    A projected study is ~550 B against 17.3 KB full (notes §3), so one pass serves any
    group-by: a secondary group-by costs nothing extra.
    """
    paths: list[str] = ["NCTId", ENROLLMENT_PATH]
    for dim in REGISTRY.values():
        paths.append(dim.record_path.replace("[]", ""))
    # Branch nodes so list children (interventions, locations) come through for network mode.
    paths.extend(
        [
            "protocolSection.armsInterventionsModule.interventions",
            "protocolSection.conditionsModule.conditions",
            "protocolSection.sponsorCollaboratorsModule.leadSponsor",
            "protocolSection.designModule",
            "protocolSection.statusModule",
            "protocolSection.identificationModule",
        ]
    )
    # Preserve order, drop duplicates.
    return "|".join(dict.fromkeys(paths))


async def fetch_all(ctx: RunContext, params: Mapping[str, str]) -> list[dict[str, Any]]:
    """Page the result set serially. Every page repeats every non-paging parameter."""
    page_params = dict(params)
    page_params["pageSize"] = str(PAGE_SIZE)
    page_params["fields"] = fields_projection()
    # countTotal is a paging-trio exception; omit it on record pages so we don't pay for it.
    page_params.pop("countTotal", None)

    studies: list[dict[str, Any]] = []
    token: str | None = None
    pages = 0

    while True:
        # Each page is one upstream request; ≤2 for the threshold, charged up front by the caller
        # when the budget is known, or spent per page here for honesty.
        ctx.spend(1, f"record-mode page {pages + 1}")
        page = await ctx.client.page(page_params, page_token=token)
        studies.extend(page.studies)
        pages += 1
        if page.next_page_token is None:
            break
        token = page.next_page_token

    await ctx.assert_data_unchanged()
    return studies


def aggregate(
    studies: Sequence[Mapping[str, Any]],
    plan: AnalysisPlan,
    dim: Dimension,
    ctx: RunContext,
) -> BucketSet:
    """Group `studies` by `dim` into a BucketSet. List dimensions contribute to every value."""
    # Starts empty, like every other mode: `analyze` concatenates ctx.warnings and
    # bucketset.warnings, so seeding from ctx here printed the fetch-drift warning twice.
    warnings: list[str] = []
    assumptions: list[str] = []

    enrollments_by_nct = _enrollment_map(studies)
    if plan.metric is not Metric.STUDY_COUNT:
        enrollments_by_nct, clamp_value, n_clamped = winsorize(enrollments_by_nct)
        if n_clamped:
            assumptions.append(
                f"Enrollment winsorized at the 99th percentile ({clamp_value:,}); "
                f"{n_clamped:,} studies were clamped. Raw enrollment is never plotted."
            )

    counts: Counter[str] = Counter()
    metric_values: dict[str, list[float]] = {}
    members: dict[str, list[Mapping[str, Any]]] = {}
    unclassified = 0

    for study in studies:
        keys = membership_keys(study, dim)
        if keys is None:
            unclassified += 1
            continue

        nct = _safe_nct(study)
        enrollment = enrollments_by_nct.get(nct) if nct else None

        for key in keys:
            counts[key] += 1
            members.setdefault(key, []).append(study)
            if plan.metric is not Metric.STUDY_COUNT and enrollment is not None:
                metric_values.setdefault(key, []).append(float(enrollment))

    ctx.assumptions.extend(assumptions)

    # Citation sampling is free: no extra upstream requests.
    per_datum = ctx.options.citations_per_datum if ctx.options.include_citations else 0
    if per_datum > 0 and members:
        ctx.assumptions.append(ORDERING_ASSUMPTION)

    buckets: list[Bucket] = []
    ordered_keys = _ordered_keys(counts, dim, ctx)
    complete = len(ordered_keys) <= ctx.options.max_buckets
    if len(ordered_keys) > ctx.options.max_buckets:
        warnings.append(
            f"{dim.key} has {len(ordered_keys)} buckets; showing the first "
            f"{ctx.options.max_buckets} because options.max_buckets is {ctx.options.max_buckets}."
        )
        ordered_keys = ordered_keys[: ctx.options.max_buckets]

    for key in ordered_keys:
        value = _metric_value(plan.metric, counts[key], metric_values.get(key, []))
        citations: list[Citation] = []
        note: str | None = None
        if per_datum > 0 and counts[key] > 0:
            citations, note = citations_from_records(members[key], dim, per_datum, counts[key])
        buckets.append(
            Bucket(
                key=key,
                label=_label(dim, key, ctx),
                value=value,
                exactness="exact",
                citations=citations,
                citation_note=note,
            )
        )

    return BucketSet(
        buckets=buckets,
        total=len(studies),
        unclassified=unclassified,
        semantics="partition" if dim.partition else "overlapping",
        mode=MODE_NAME,
        aggregation_capped=not complete,
        warnings=warnings,
    )


async def run(
    plan: AnalysisPlan,
    dim: Dimension,
    ctx: RunContext,
    *,
    params: Mapping[str, str],
    total: int,
) -> tuple[BucketSet, list[dict[str, Any]]]:
    """Fetch and aggregate. Returns the bucket set and the in-memory studies (for network)."""
    studies = await fetch_all(ctx, params)
    if len(studies) != total:
        # Upstream can drift between preflight and the last page; surface it rather than
        # silently disagreeing with meta.total_matching_studies.
        ctx.warnings.append(
            f"Record-mode fetched {len(studies):,} studies but preflight counted {total:,}; "
            f"using the fetched set."
        )
    return aggregate(studies, plan, dim, ctx), studies


def membership_keys(study: Mapping[str, Any], dim: Dimension) -> list[str] | None:
    """Return the bucket keys this study belongs to, or `None` if the field is absent.

    Explicit `["NA"]` is a value. A missing key is unclassified. Merging them is the notes §6.1
    trap this function exists to refuse.
    """
    try:
        raw = _raw_membership(study, dim)
    except KeyError:
        return None

    if raw is None:
        return None

    if is_temporal(dim):
        year = _year_of(raw if isinstance(raw, str) else None)
        return [str(year)] if year is not None else None

    if dim.key in QUANTITATIVE_KEYS:
        # A non-numeric or absent value is unclassified, never bin zero: notes §6.4 found
        # enrollment missing on 7,133 studies, and folding those into "0-10" would invent a
        # spike of tiny trials that do not exist.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return [bin_key(int(raw))]

    if isinstance(raw, list):
        if not raw:
            return None
        return [str(item) for item in raw]

    return [str(raw)]


def _raw_membership(study: Mapping[str, Any], dim: Dimension) -> Any:
    """Read the membership field; list-paths return the full list of child values."""
    path = dim.record_path
    if "[]" in path:
        return value_at(study, path)
    # Non-list path: walk without inventing a missing leaf as empty.
    current: Any = study
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            raise KeyError(segment)
        current = current[segment]
    return current


def _year_of(value: str | None) -> int | None:
    """Accept `yyyy-MM` and `yyyy-MM-dd`. A missing/unparseable date is unclassified, not 1900."""
    if not value or len(value) < 4 or not value[:4].isdigit():
        return None
    year = int(value[:4])
    if year < 1900 or year > 2100:
        return None
    return year


def _enrollment_map(studies: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for study in studies:
        nct = _safe_nct(study)
        if nct is None:
            continue
        try:
            raw = value_at(study, ENROLLMENT_PATH)
        except KeyError:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        out[nct] = int(raw)
    return out


def winsorize(values: Mapping[str, int]) -> tuple[dict[str, int], int, int]:
    """Clamp at the 99th percentile. Returns `(clamped, threshold, n_clamped)`."""
    if not values:
        return {}, 0, 0
    ordered = sorted(values.values())
    # Nearest-rank 99th percentile (ceil(p · n), 1-indexed → 0-indexed).
    index = min(len(ordered) - 1, max(0, math.ceil(0.99 * len(ordered)) - 1))
    threshold = ordered[index]
    clamped = {nct: min(value, threshold) for nct, value in values.items()}
    n_clamped = sum(1 for value in values.values() if value > threshold)
    return clamped, threshold, n_clamped


def _metric_value(metric: Metric, study_count: int, enrollments: list[float]) -> float:
    if metric is Metric.STUDY_COUNT:
        return float(study_count)
    if not enrollments:
        return 0.0
    if metric is Metric.ENROLLMENT_SUM:
        return float(sum(enrollments))
    return float(statistics.median(enrollments))


def _ordered_keys(counts: Counter[str], dim: Dimension, ctx: RunContext) -> list[str]:
    if dim.enum_name is not None:
        live = set(counts)
        order = [value for value in ctx.vocab.sort_order(dim.enum_name) if value in live]
        leftovers = sorted(live - set(order))
        return order + leftovers
    if is_temporal(dim):
        return sorted(counts, key=lambda key: int(key) if key.isdigit() else key)
    # Open vocabulary: value descending, key ascending.
    return sorted(counts, key=lambda key: (-counts[key], key))


def _label(dim: Dimension, key: str, ctx: RunContext) -> str:
    if dim.enum_name is None:
        return key
    return ctx.vocab.label(dim.enum_name, key)


def _safe_nct(study: Mapping[str, Any]) -> str | None:
    try:
        return nct_id_of(study)
    except KeyError:
        return None


def enrollment_comparison_unplannable(threshold: int) -> Any:
    """A comparison of enrollment, refused before any preflight has run.

    Separate from `enrollment_unplannable` because that one names the match count, and here
    there is none: refusing before the per-series preflights is the whole point, so quoting
    "this query matches 0 studies" would be a fabricated number attached to an honest refusal.
    """
    from app.errors import CheironError, ErrorCode

    return CheironError(
        ErrorCode.UNPLANNABLE_QUERY,
        f"An enrollment metric cannot be compared across series: each series needs its own "
        f"complete_records read (\u2264{threshold:,} studies), which is not known until each has "
        f"been counted separately.",
        details=[
            {
                "record_mode_threshold": threshold,
                "suggestion": ("Ask for one series at a time, or compare with metric study_count."),
            }
        ],
    )


def enrollment_unplannable(total: int, threshold: int) -> Any:
    """Above the record-mode threshold, enrollment metrics cannot be served (BUILD-PLAN §6.3)."""
    from app.errors import CheironError, ErrorCode

    return CheironError(
        ErrorCode.UNPLANNABLE_QUERY,
        f"Enrollment metrics need per-record values, which require complete_records mode "
        f"(≤{threshold:,} studies). This query matches {total:,} studies.",
        details=[
            {
                "suggestion": (
                    "Narrow the filters so fewer than "
                    f"{threshold:,} studies match, or use metric study_count."
                ),
                "total_matching_studies": total,
                "record_mode_threshold": threshold,
            }
        ],
    )
