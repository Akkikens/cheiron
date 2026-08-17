"""The single place Essie expressions are constructed. SPEC §5.3, notes §2.

Every count in every chart is only as trustworthy as the predicate behind it, so this module
is written so the dangerous expressions cannot be built at all:

- `full_match` requires an `area`, because T01 measured that a bare
  `COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` is valid Essie, returns HTTP 200, and counts
  **4591** against the correct 1841. Unscoped is not an error upstream: it is a wrong answer
  that looks like a right one.
- `escape` is applied to user text unconditionally. Left off, notes §2 shows
  `AREA[LeadSponsorName]COVERAGE[FullMatch]"Merck" OR AREA[Phase]PHASE3` returning 49,659:
  the injected clause executes.

The legacy facet-filter parameter is never used anywhere in this codebase: it returns 0 with
HTTP 200 on a bad option key (notes §2), and a test greps `app/` to keep it that way.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Final, Literal

from app.constants import FULL_MATCH_OP

OPERATOR_KEYWORDS: Final = (
    "AND",
    "OR",
    "NOT",
    "AREA",
    "SEARCH",
    "RANGE",
    "MISSING",
    "COVERAGE",
    "COVER",
    "EXPANSION",
    "FullMatch",
    "DISTANCE",
    "ALL",
)

# Case-sensitive by measurement: lowercase `and`/`or`/`not` are ordinary search terms
# (486 hits vs 9,964), so only the exact spellings need escaping. Word-bounded so that
# `SMALLPOX` does not become `SM\ALLPOX`.
_KEYWORD_RE: Final = re.compile(r"\b(" + "|".join(OPERATOR_KEYWORDS) + r")\b")

MIN: Final = "MIN"
MAX: Final = "MAX"

LowerBound = int | Literal["MIN"]
UpperBound = int | Literal["MAX"]


class Essie:
    """Builders for `filter.advanced` expressions. All output is already escaped."""

    @staticmethod
    def escape(text: str) -> str:
        """Neutralise Essie syntax in user-supplied text.

        Backslashes first, then quotes, then keywords: any other order double-escapes the
        backslashes this function itself introduces. Over-escaping is safe: notes §2 records
        that a backslash on a non-operator is ignored upstream.
        """
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return _KEYWORD_RE.sub(r"\\\1", escaped)

    @staticmethod
    def _require_area(area: str) -> str:
        if not area or not area.strip():
            raise ValueError(
                "area is required: an unscoped predicate matches the default search areas "
                "and returns a plausible wrong count (notes §2, 4591 vs 1841)."
            )
        return area

    @staticmethod
    def field_eq(area: str, value: str) -> str:
        """`AREA[Phase]PHASE2`. Closed-enum and partial-date buckets."""
        return f"AREA[{Essie._require_area(area)}]{Essie.escape(value)}"

    @staticmethod
    def full_match(area: str, value: str) -> str:
        """`AREA[LeadSponsorName]COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` → 1841.

        `area` is positional and required; there is deliberately no way to omit it.
        """
        scoped = Essie._require_area(area)
        return f'AREA[{scoped}]{FULL_MATCH_OP}[FullMatch]"{Essie.escape(value)}"'

    @staticmethod
    def date_range(area: str, start: date, end: date) -> str:
        """`AREA[StartDate]RANGE[2020-01-01,2020-12-31]`."""
        if end < start:
            raise ValueError(f"date_range end {end} precedes start {start}")
        return f"AREA[{Essie._require_area(area)}]RANGE[{start.isoformat()},{end.isoformat()}]"

    @staticmethod
    def numeric_range(area: str, lo: LowerBound, hi: UpperBound) -> str:
        """`AREA[EnrollmentCount]RANGE[500,MAX]`."""
        if isinstance(lo, int) and isinstance(hi, int) and hi < lo:
            raise ValueError(f"numeric_range hi {hi} precedes lo {lo}")
        return f"AREA[{Essie._require_area(area)}]RANGE[{lo},{hi}]"

    @staticmethod
    def missing(area: str) -> str:
        """`AREA[Phase]MISSING`: the field is absent, which is not the same as `NA`."""
        return f"AREA[{Essie._require_area(area)}]MISSING"

    @staticmethod
    def has_value(area: str) -> str:
        """`AREA[ResultsFirstPostDate]RANGE[MIN,MAX]`: the field is present."""
        return f"AREA[{Essie._require_area(area)}]RANGE[{MIN},{MAX}]"

    @staticmethod
    def all_() -> str:
        return "ALL"

    @staticmethod
    def phrase(text: str) -> str:
        """A quoted free-text term: `"breast cancer"`.

        Deliberately unscoped, unlike `full_match`. A phrase is evaluated against whichever
        parameter carries it: `query.cond` gives 16,538 for `"breast cancer"` while
        `filter.advanced` gives 17,819 (notes §2), and that breadth is the point of a search
        term. `full_match` is the opposite: it *claims* to be an exact field match, so an
        unscoped one is a wrong answer rather than a broader question.
        """
        return f'"{Essie.escape(text)}"'

    @staticmethod
    def and_(*exprs: str) -> str:
        return Essie._join("AND", exprs)

    @staticmethod
    def or_(*exprs: str) -> str:
        return Essie._join("OR", exprs)

    @staticmethod
    def not_(include: str, exclude: str) -> str:
        """`(A) NOT (B)`. Essie's `NOT` is **exclusion**, not logical negation.

        It is binary for that reason: it narrows `include` by removing `exclude`, so there is
        no meaningful unary form to offer.
        """
        if not include or not exclude:
            raise ValueError("NOT needs both an expression to keep and one to exclude")
        left = include if _is_wrapped(include) else f"({include})"
        right = exclude if _is_wrapped(exclude) else f"({exclude})"
        return f"({left} NOT {right})"

    @staticmethod
    def distance(area: str, lat: float, lon: float, radius: int, unit: str = "mi") -> str:
        """`AREA[LocationGeoPoint]DISTANCE[42.36,-71.06,50mi]`.

        Square brackets, not parens: `DISTANCE(...)` leaks a raw Java parser exception
        (notes §2). `filter.geo` uses the paren form: two syntaxes for one concept.
        """
        if unit not in {"mi", "km"}:
            raise ValueError(f"radius unit must be mi or km, got {unit!r}")
        return f"AREA[{Essie._require_area(area)}]DISTANCE[{lat},{lon},{radius}{unit}]"

    @staticmethod
    def _join(operator: str, exprs: tuple[str, ...]) -> str:
        """Always parenthesised. Documented precedence is never relied on."""
        terms = [expr for expr in exprs if expr]
        if not terms:
            raise ValueError(f"{operator} needs at least one operand")
        if len(terms) == 1:
            return terms[0]
        wrapped = [term if _is_wrapped(term) else f"({term})" for term in terms]
        return "(" + f" {operator} ".join(wrapped) + ")"


def _is_wrapped(expression: str) -> bool:
    """True when the whole expression is already inside one matching pair of parentheses.

    Only used to avoid `((( )))` noise in `meta.api_query_log`; the grouping itself is never
    left to precedence.
    """
    if not expression.startswith("(") or not expression.endswith(")"):
        return False

    depth = 0
    for index, character in enumerate(expression):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return index == len(expression) - 1
    return False
