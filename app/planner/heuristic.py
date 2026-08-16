"""Rule-based planner. SPEC §5.5.

Built before the LLM planner on purpose: it is the deterministic reference every LLM path is
tested against, and it keeps the service fully functional with `LLM_ENABLED=false` and no API
key present at all.

**Precedence is observable behaviour, so it is fixed and documented.** Templates are tried in
the order below and the first keyword hit wins:

1. phase 2. status 3. year 4. country 5. sponsor

The consequence worth knowing before you file it as a bug: *"trials by phase over time"* hits
`phase` first and returns a phase distribution, not a trend. That is the honest behaviour of a
keyword matcher, and picking apart which of two named dimensions the caller meant is exactly
the judgement the LLM planner exists for.

What this planner refuses to do is guess. If `drug_name` is absent it does not mine the question
for a drug name — a wrong filter produces a confident wrong chart, which is worse than asking
the caller to be explicit. `filters.term` is therefore always `None`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.ctg.vocab import Vocabulary
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan, Bin, GroupBy, Intent, Metric, StudyFilter
from app.models.request import AnalyzeRequest
from app.planner.base import PlanResult


@dataclass(frozen=True)
class Template:
    key: str
    keywords: tuple[str, ...]
    intent: Intent
    dimension: str
    phrasing: str
    bin: Bin | None = None
    example: str = ""


TEMPLATES: Final[tuple[Template, ...]] = (
    Template(
        key="phase",
        keywords=("phase",),
        intent=Intent.DISTRIBUTION,
        dimension="phase",
        phrasing="Distribution of {subject} across trial phases",
        example="How many trials by phase?",
    ),
    Template(
        key="status",
        keywords=("status", "recruiting", "completed", "active"),
        intent=Intent.DISTRIBUTION,
        dimension="overall_status",
        phrasing="Distribution of {subject} across recruitment statuses",
        example="What is the recruitment status of these trials?",
    ),
    Template(
        key="year",
        keywords=("over time", "trend", "by year", "since", "changed", "growth"),
        intent=Intent.TREND,
        dimension="start_year",
        phrasing="Annual count of {subject} by start year",
        bin=Bin(size=1),
        example="How has trial activity changed over time?",
    ),
    Template(
        key="country",
        keywords=("country", "countries", "where", "geograph", "region"),
        intent=Intent.GEO,
        dimension="country",
        phrasing="Geographic distribution of {subject} by country",
        example="Which countries run the most trials?",
    ),
    Template(
        key="sponsor",
        keywords=("sponsor", "company", "who is running", "funder"),
        intent=Intent.DISTRIBUTION,
        dimension="lead_sponsor",
        phrasing="Distribution of {subject} by lead sponsor",
        example="Who sponsors the most trials?",
    ),
)


class HeuristicPlanner:
    """Satisfies `Planner`. Async only because the protocol is; it never awaits."""

    async def plan(self, req: AnalyzeRequest, vocab: Vocabulary) -> PlanResult:
        template = match(req.query)
        if template is None:
            raise CheironError(
                ErrorCode.UNPLANNABLE_QUERY,
                "This question cannot be answered from clinical trial metadata by the "
                "deterministic planner.",
                details=[
                    {"suggestion": candidate.example, "groups_by": candidate.dimension}
                    for candidate in TEMPLATES
                ],
            )

        filters = filters_from_request(req)
        return PlanResult(
            plan=AnalysisPlan(
                intent=template.intent,
                filters=filters,
                series=[],
                group_by=GroupBy(dimension=template.dimension, bin=template.bin),
                secondary_group_by=None,
                metric=Metric.STUDY_COUNT,
                # Chart choice belongs to the registry (SPEC §6.1), not the planner.
                viz_hint=None,
                interpretation=interpretation_for(template, filters, vocab),
            ),
            planner="heuristic_fallback",
            attempts=1,
        )


def match(query: str) -> Template | None:
    haystack = query.lower()
    for template in TEMPLATES:
        if any(keyword in haystack for keyword in template.keywords):
            return template
    return None


def filters_from_request(req: AnalyzeRequest) -> StudyFilter:
    """Structured fields only. Nothing is inferred from the question text."""
    return StudyFilter(
        condition=req.condition,
        intervention=req.drug_name,
        sponsor=req.sponsor,
        term=None,
        country=req.country,
        phase=list(req.phase or []),
        status=list(req.status or []),
        study_type=req.study_type,
        start_year=req.start_year,
        end_year=req.end_year,
    )


def interpretation_for(template: Template, filters: StudyFilter, vocab: Vocabulary) -> str:
    """Deterministic prose over the resolved filters. Never model prose, never a count."""
    return f"{template.phrasing.format(subject=_subject(filters, vocab))}."


def _subject(filters: StudyFilter, vocab: Vocabulary) -> str:
    qualifiers: list[str] = []

    if filters.study_type:
        qualifiers.append(vocab.label("StudyType", filters.study_type).lower())
    qualifiers.append("clinical trials")

    if filters.intervention:
        qualifiers.append(f"studying {filters.intervention}")
    if filters.condition:
        qualifiers.append(f"in {filters.condition}")
    if filters.sponsor:
        qualifiers.append(f"led by {filters.sponsor}")
    if filters.country:
        qualifiers.append(f"with a location in {filters.country}")
    if filters.phase:
        labels = [vocab.label("Phase", value) for value in filters.phase]
        qualifiers.append(f"in {_join(labels)}")
    if filters.status:
        labels = [vocab.label("Status", value) for value in filters.status]
        qualifiers.append(f"with status {_join(labels)}")

    span = _year_span(filters)
    if span:
        qualifiers.append(span)

    return " ".join(qualifiers)


def _year_span(filters: StudyFilter) -> str:
    start, end = filters.start_year, filters.end_year
    if start and end:
        return f"starting {start}-{end}" if start != end else f"starting in {start}"
    if start:
        return f"starting {start} or later"
    if end:
        return f"starting {end} or earlier"
    return ""


def _join(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} or {values[-1]}"
