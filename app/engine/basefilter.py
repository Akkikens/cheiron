"""The base filter: the parameters every count in one analysis shares.

**`query.*` and `filter.advanced` are not interchangeable, and merging them changes the
answer.** T03 measured the same expression under both: `(head OR neck) AND pain NOT cancer` is
2,075 under `query.cond` and 9,964 under `filter.advanced`; `"breast cancer"` is 16,538 vs
17,819. The `query.*` params are scoped to a search area, `filter.advanced` searches unscoped.

So the two halves stay separate:

- **free-text filters go to `query.*`** per SPEC §2.1 — `intervention` → `query.intr`,
  `condition` → `query.cond`, `sponsor` → `query.lead`, `term` → `query.term`;
- **structured constraints and bucket predicates go to `filter.advanced`** via the builder.

This is load-bearing, not stylistic. SPEC A1's total of 2,927 was measured with
`query.intr=pembrolizumab`. Moving that term into `filter.advanced` looks like a simplification
and silently changes the total, which invalidates every bucket underneath it.
"""

from __future__ import annotations

from datetime import date

from app.ctg.essie import Essie
from app.engine.dimensions import REGISTRY
from app.models.plan import StudyFilter

SPONSOR_ASSUMPTION = (
    "sponsor was matched against the lead sponsor only (query.lead), not lead plus "
    "collaborators (query.spons); the two differ materially — Pfizer returns 3,862 against "
    "6,064."
)
"""SPEC §2.1 settles the mapping and requires it to be disclosed on every response using it."""

# Free-text filter field -> the `query.*` parameter that carries it (SPEC §2.1).
#
# `country` is deliberately absent: SPEC §2.1 maps it to `AREA[LocationCountry]`, not to a
# `query.*` param, and it is applied as an exact match below.
QUERY_PARAM_BY_FIELD: dict[str, str] = {
    "intervention": "query.intr",
    "condition": "query.cond",
    "sponsor": "query.lead",
    "term": "query.term",
}


def base_filter(filters: StudyFilter) -> tuple[dict[str, str], list[str]]:
    """Split a `StudyFilter` into upstream params and the assumptions worth disclosing.

    Returns `(params, assumptions)`. `params` may contain `filter.advanced`; bucket predicates
    are ANDed onto it later by `with_predicate`, never by string concatenation at the call site.
    """
    params: dict[str, str] = {}
    assumptions: list[str] = []

    for field_name, param in QUERY_PARAM_BY_FIELD.items():
        value = getattr(filters, field_name)
        if value:
            params[param] = value

    if filters.sponsor:
        assumptions.append(SPONSOR_ASSUMPTION)

    advanced = _structured_predicates(filters)
    if advanced:
        params["filter.advanced"] = Essie.and_(*advanced)

    return params, assumptions


def _structured_predicates(filters: StudyFilter) -> list[str]:
    """The closed-vocabulary and date constraints, which belong in Essie rather than a query."""
    predicates: list[str] = []

    if filters.phase:
        predicates.append(
            Essie.or_(*(Essie.field_eq(REGISTRY["phase"].area, value) for value in filters.phase))
        )
    if filters.status:
        predicates.append(
            Essie.or_(
                *(
                    Essie.field_eq(REGISTRY["overall_status"].area, value)
                    for value in filters.status
                )
            )
        )
    if filters.study_type:
        predicates.append(Essie.field_eq(REGISTRY["study_type"].area, filters.study_type))

    if filters.country:
        # SPEC §5.3's mandated country predicate. Worth knowing that it is not doing the work
        # here that it does for sponsors: on `LocationCountry` the bare and exact forms agree on
        # every name measured, including Niger against Nigeria (notes §6.7). Kept because it is
        # the spec'd predicate and cannot be looser than the alternative.
        predicates.append(Essie.full_match(REGISTRY["country"].area, filters.country))

    span = year_span(filters)
    if span is not None:
        predicates.append(Essie.date_range(REGISTRY["start_year"].area, *span))

    return predicates


def year_span(filters: StudyFilter) -> tuple[date, date] | None:
    """A start-date range from whichever year bounds are set.

    An open end is expressed as a wide date rather than omitted, because `RANGE` needs both
    edges and `MAX` would include the 2099 garbage that notes §6.3 found at the top of the
    corpus.
    """
    if filters.start_year is None and filters.end_year is None:
        return None

    start_year = filters.start_year if filters.start_year is not None else 1900
    # An open end defaults to this year, but never earlier than the start: a request for
    # `start_year=2030` with no end is well-formed and asks about planned trials, and inverting
    # the range would raise out of `Essie.date_range` and surface as a 500 on a valid question.
    default_end = max(date.today().year, start_year)
    end_year = filters.end_year if filters.end_year is not None else default_end
    return date(start_year, 1, 1), date(end_year, 12, 31)


def with_predicate(params: dict[str, str], predicate: str) -> dict[str, str]:
    """AND a bucket predicate onto the base filter's `filter.advanced`.

    Composition goes through `Essie.and_` so the result is parenthesised: appending
    `" AND " + predicate` by hand would let the base filter's own `OR` re-associate and quietly
    widen the bucket.
    """
    merged = dict(params)
    existing = merged.get("filter.advanced")
    merged["filter.advanced"] = Essie.and_(existing, predicate) if existing else predicate
    return merged
