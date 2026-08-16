"""One cheap request that decides how the whole analysis runs. SPEC §5.2.

**Why the threshold is 2,000.** `pageToken` chains are strictly serial (notes §1): a token is
only valid for the exact parameter set that produced it, so pages cannot be fetched in parallel
or by offset. Paging 2,000 studies is ~2 round trips and about a second; 50,000 is ~50 round
trips and 25-50 seconds, which does not fit on a request path. So below the threshold the
engine reads every record and gets exactness, all dimensions, and citations for free; above it
the engine stops reading records and asks the server to count instead, at a cost independent of
result size.

The number is a property of upstream's paging, not a tuning knob — that is why it is derived
from `RECORD_MODE_THRESHOLD` in one place and recorded here rather than in a comment at the
call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.engine.basefilter import base_filter
from app.engine.context import RunContext
from app.engine.dimensions import Dimension
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan

AggregationModeName = Literal["complete_records", "server_counts", "sampled_then_confirmed"]


@dataclass(frozen=True)
class Preflight:
    total: int
    mode: AggregationModeName
    data_timestamp: str
    params: dict[str, str]
    assumptions: list[str]


def select_mode(total: int, dim: Dimension, threshold: int) -> AggregationModeName:
    """SPEC §5.2's table, as one expression with no hidden fourth case."""
    if total <= threshold:
        return "complete_records"
    if dim.enum_name is not None:
        return "server_counts"
    return "sampled_then_confirmed"


async def preflight(
    plan: AnalysisPlan, dim: Dimension, ctx: RunContext, *, threshold: int
) -> Preflight:
    """One `countTotal` request on the base filter, then pick the mode."""
    params, assumptions = base_filter(plan.filters)

    ctx.spend(1, "the preflight count")
    total = await ctx.client.count(params)

    return Preflight(
        total=total,
        mode=select_mode(total, dim, threshold),
        data_timestamp=ctx.data_timestamp,
        params=params,
        assumptions=assumptions,
    )


def unimplemented_mode(mode: AggregationModeName, total: int, dim: Dimension) -> CheironError:
    """Modes T10 and T11 own. A clear refusal, never a silent downgrade to a mode that lies.

    Downgrading `sampled_then_confirmed` to `server_counts` would be impossible anyway — there
    is no enum to fan out over — but downgrading it to a truncated `complete_records` read would
    return a relevance-ranked slice that looks authoritative and is not (SPEC §5.4).
    """
    reason = {
        "complete_records": (
            f"grouping by {dim.key!r} over {total:,} studies needs the full-record reader, "
            "which is not implemented yet"
        ),
        "sampled_then_confirmed": (
            f"{dim.key!r} has an open vocabulary, so grouping {total:,} studies needs the "
            "sampling mode, which is not implemented yet"
        ),
    }.get(mode, f"{mode} is not implemented yet")

    return CheironError(
        ErrorCode.UNPLANNABLE_QUERY,
        f"This question cannot be answered exactly right now: {reason}.",
        details=[
            {
                "aggregation_mode": mode,
                "total_matching_studies": total,
                "dimension": dim.key,
                "suggestion": (
                    "Narrow the filters so fewer studies match, or group by a closed-vocabulary "
                    "dimension such as phase, overall_status, study_type, or sponsor_class."
                ),
            }
        ],
    )
