"""Per-bucket citation sampling. SPEC §4.2.

The discipline matters more than the plumbing. A citation that points at the wrong field is
worse than no citation, because it looks rigorous — especially when upstream synonym expansion
puts a study in a bucket whose query string never appears in the record.

**Failure semantics are the opposite of the count fan-out.** T06 made a single bucket failure
fatal because a missing bar is a lie about the data. A missing citation is not: it is missing
evidence for a number that is still exact. So a citation fetch failure drops that bucket's
citations, warns naming the bucket, and keeps the chart.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.engine.basefilter import with_predicate
from app.engine.context import RunContext
from app.engine.dimensions import Dimension
from app.models.response import Citation

STUDY_URL = "https://clinicaltrials.gov/study/{nct_id}"

ORDERING_ASSUMPTION = (
    "Citations are the first N studies returned by the upstream API; ordering is not "
    "guaranteed stable across requests."
)
"""There is no default sort upstream, and text fields reject `sort` (notes §3). Saying so
avoids promising a stability the API cannot give — a citation set that silently reshuffles
between identical requests would erode trust in the numbers next to it."""

# Free-text narrative fields. Citing any of these is the failure mode Rule 2 exists to prevent:
# synonym expansion can put a study in a bucket whose query string never appears in the record.
_FORBIDDEN_FIELD_FRAGMENTS = frozenset(
    {
        "briefsummary",
        "detaileddescription",
        "brieftitle",
        "officialtitle",
        "eligibilitycriteria",
    }
)


def citation_note(n_returned: int, contributing: int) -> str:
    """SPEC §4.2 wording, with thousands separators."""
    if contributing <= n_returned:
        return f"all {contributing:,} contributing studies"
    return f"{n_returned:,} of {contributing:,} contributing studies"


def fields_projection(dim: Dimension) -> str:
    """`NCTId` plus the structured membership field. Never a narrative leaf."""
    path = dim.record_path.replace("[]", "")
    return f"NCTId|{path}"


def value_at(record: Mapping[str, Any], path: str) -> Any:
    """Read `dim.record_path` from a study, expanding `[]` into a list of child values."""
    current: Any = record
    segments = path.split(".")
    for index, segment in enumerate(segments):
        listed = segment.endswith("[]")
        key = segment[:-2] if listed else segment
        if not isinstance(current, Mapping) or key not in current:
            raise KeyError(f"{path}: missing {key!r}")
        current = current[key]
        if listed:
            if not isinstance(current, list):
                raise KeyError(f"{path}: {key!r} is not a list")
            rest = ".".join(segments[index + 1 :])
            if not rest:
                return current
            return [value_at(item, rest) for item in current if isinstance(item, Mapping)]
    return current


def serialize_excerpt(value: Any) -> str:
    """Verbatim serialization: scalars as they appear; lists/dicts as compact JSON."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def nct_id_of(study: Mapping[str, Any]) -> str:
    direct = study.get("nctId") or study.get("NCTId")
    if isinstance(direct, str) and direct:
        return direct
    try:
        return str(study["protocolSection"]["identificationModule"]["nctId"])
    except (KeyError, TypeError) as exc:
        raise KeyError("study has no NCTId") from exc


def citations_from_records(
    records: Sequence[Mapping[str, Any]], dim: Dimension, n: int, contributing: int
) -> tuple[list[Citation], str]:
    """Build citations from already-fetched studies. T10's in-memory page lands here."""
    sampled = list(records[:n])
    citations = [_citation(study, dim) for study in sampled]
    return citations, citation_note(len(citations), contributing)


def _citation(study: Mapping[str, Any], dim: Dimension) -> Citation:
    field = dim.record_path
    _assert_not_free_text(field)
    excerpt = serialize_excerpt(value_at(study, field))
    nct_id = nct_id_of(study)
    return Citation(
        nct_id=nct_id,
        field=field,
        excerpt=excerpt,
        url=STUDY_URL.format(nct_id=nct_id),
    )


def _assert_not_free_text(field: str) -> None:
    lowered = field.lower()
    for fragment in _FORBIDDEN_FIELD_FRAGMENTS:
        if fragment in lowered:
            raise ValueError(
                f"citation field {field!r} names a free-text narrative; citations must use "
                f"the structured membership path (SPEC §4.2 rule 2)"
            )


async def sample_citations(
    bucket_predicate: str,
    dim: Dimension,
    n: int,
    ctx: RunContext,
    *,
    contributing: int,
    base_params: Mapping[str, str] | None = None,
    records: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[Citation], str]:
    """Sample up to `n` citations for one bucket.

    When `records` is provided, no upstream request is made — that is the T10 path. When it is
    omitted, one projected page is fetched under the bucket predicate.
    """
    if n <= 0 or contributing <= 0:
        return [], citation_note(0, contributing)

    if records is not None:
        return citations_from_records(records, dim, n, contributing)

    params = with_predicate(dict(base_params or {}), bucket_predicate)
    params["pageSize"] = str(min(n, contributing))
    params["fields"] = fields_projection(dim)

    page = await ctx.client.page(params)
    return citations_from_records(page.studies, dim, n, contributing)


def plan_citation_budget(
    ctx: RunContext, *, bucket_count: int, count_cost: int
) -> tuple[int, list[str]]:
    """How many citation fetches we can afford after reserving the count wave.

    Citations are the first thing cut when the budget is tight: degrade the evidence, keep the
    numbers. Returns `(fetches_afforded, warnings)`.
    """
    warnings: list[str] = []
    if not ctx.options.include_citations or ctx.options.citations_per_datum <= 0:
        return 0, warnings

    available = ctx.upstream_budget - ctx.spent
    after_counts = available - count_cost
    if after_counts <= 0:
        warnings.append(
            f"Upstream budget has room for the {count_cost} count requests but not for "
            f"citations; returning exact numbers without evidence."
        )
        return 0, warnings

    if ctx.remaining_ms <= 0:
        warnings.append(
            "Request deadline has no time left for citation fetches; returning exact numbers "
            "without evidence."
        )
        return 0, warnings

    afforded = min(bucket_count, after_counts)
    if afforded < bucket_count:
        warnings.append(
            f"Citation fetches cut from {bucket_count} to {afforded} to stay within the "
            f"{ctx.upstream_budget} upstream-request budget; some buckets have no citations."
        )
    return afforded, warnings
