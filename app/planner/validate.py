"""Plan validation and hard-constraint enforcement. SPEC §3.

Every message here is read twice: once by a human debugging, and once by the model as repair
input (SPEC §3, T09). So each one names the offending field, the offending value, and something
valid to use instead. A message like "invalid group_by" is useless to both readers.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.engine.dimensions import QUANTITATIVE_KEYS, REGISTRY, TEMPORAL_KEYS, is_temporal
from app.models.plan import AnalysisPlan, Intent, Metric, StudyFilter

if TYPE_CHECKING:
    from app.ctg.vocab import Vocabulary
    from app.models.request import AnalyzeRequest

MIN_YEAR = 1900
MAX_YEAR = 2100

ENUM_BY_FILTER_FIELD: dict[str, str] = {
    "phase": "Phase",
    "status": "Status",
    "study_type": "StudyType",
}

_NUMBER = re.compile(r"\d[\d,]*")


def _dimension_options() -> str:
    return ", ".join(sorted(REGISTRY))


def _check_dimensions(plan: AnalysisPlan) -> list[str]:
    errors: list[str] = []
    named = [("group_by", plan.group_by)]
    if plan.secondary_group_by is not None:
        named.append(("secondary_group_by", plan.secondary_group_by))

    for field_name, group_by in named:
        if group_by.dimension not in REGISTRY:
            errors.append(
                f"{field_name}.dimension is {group_by.dimension!r}, which is not a groupable "
                f"dimension. Valid dimensions: {_dimension_options()}."
            )
        elif group_by.bin is not None and not _is_binnable(group_by.dimension):
            binnable = ", ".join(sorted(TEMPORAL_KEYS | QUANTITATIVE_KEYS))
            errors.append(
                f"{field_name}.bin is set on {group_by.dimension!r}, which is not binnable. "
                f"Binning applies to temporal and quantitative dimensions ({binnable}); drop "
                f"bin, or group by one of those."
            )

    if plan.secondary_group_by is not None and plan.group_by.dimension == (
        plan.secondary_group_by.dimension
    ):
        errors.append(
            f"secondary_group_by.dimension repeats group_by.dimension "
            f"({plan.group_by.dimension!r}); use a different dimension or drop "
            "secondary_group_by."
        )
    return errors


def _is_binnable(dimension: str) -> bool:
    return is_temporal(REGISTRY[dimension]) or dimension in QUANTITATIVE_KEYS


def _check_enums(plan: AnalysisPlan, vocab: Vocabulary) -> list[str]:
    errors: list[str] = []
    scopes: list[tuple[str, StudyFilter]] = [("filters", plan.filters)]
    scopes.extend(
        (f"series[{index}].filters", series.filters) for index, series in enumerate(plan.series)
    )

    for scope, filters in scopes:
        for field_name, enum_name in ENUM_BY_FILTER_FIELD.items():
            raw = getattr(filters, field_name)
            values = raw if isinstance(raw, list) else ([] if raw is None else [raw])
            for value in values:
                if not vocab.is_valid(enum_name, value):
                    allowed = ", ".join(vocab.values(enum_name))
                    errors.append(
                        f"{scope}.{field_name} contains {value!r}, which is not a live "
                        f"{enum_name} value. Valid values: {allowed}."
                    )
    return errors


def _check_years(plan: AnalysisPlan) -> list[str]:
    errors: list[str] = []
    scopes: list[tuple[str, StudyFilter]] = [("filters", plan.filters)]
    scopes.extend(
        (f"series[{index}].filters", series.filters) for index, series in enumerate(plan.series)
    )

    for scope, filters in scopes:
        for field_name in ("start_year", "end_year"):
            year = getattr(filters, field_name)
            if year is not None and not MIN_YEAR <= year <= MAX_YEAR:
                errors.append(
                    f"{scope}.{field_name} is {year}, outside {MIN_YEAR}-{MAX_YEAR}. Use a year "
                    f"in that range or null."
                )

        start, end = filters.start_year, filters.end_year
        if start is not None and end is not None and start > end:
            errors.append(
                f"{scope}.start_year ({start}) is after {scope}.end_year ({end}). Swap them or "
                f"widen the range."
            )
    return errors


def _check_coherence(plan: AnalysisPlan) -> list[str]:
    """SPEC §3's incoherent combinations.

    `len(series) > 1` with a non-comparison intent is deliberately absent: `AnalysisPlan`
    refuses to parse it at all, with a message written for this same repair loop. Checking it
    twice would mean maintaining two wordings for one rule.
    """
    errors: list[str] = []

    if plan.intent is Intent.NETWORK and plan.metric is not Metric.STUDY_COUNT:
        errors.append(
            f"intent is 'network' but metric is {plan.metric.value!r}; a co-occurrence graph "
            f"counts trials. Set metric to 'study_count'."
        )

    if plan.intent is Intent.TREND and plan.group_by.dimension not in TEMPORAL_KEYS:
        errors.append(
            f"intent is 'trend' but group_by.dimension is {plan.group_by.dimension!r}, which is "
            f"not temporal. Group by {', '.join(sorted(TEMPORAL_KEYS))}, or change the intent."
        )

    if plan.intent is Intent.GEO and plan.group_by.dimension != "country":
        errors.append(
            f"intent is 'geo' but group_by.dimension is {plan.group_by.dimension!r}. Group by "
            f"'country', or change the intent."
        )

    if plan.intent is Intent.COMPARISON and len(plan.series) < 2:
        errors.append(
            f"intent is 'comparison' but series has {len(plan.series)} entries; a comparison "
            f"needs at least 2. Add a second series, or use intent 'distribution'."
        )

    if plan.intent is Intent.SCATTER and plan.secondary_group_by is None:
        errors.append(
            "intent is 'scatter' but secondary_group_by is null; a scatter plot needs two "
            "dimensions. Set secondary_group_by, or change the intent."
        )

    errors.extend(_check_intent_is_reachable(plan))
    return errors


def _check_intent_is_reachable(plan: AnalysisPlan) -> list[str]:
    """Refuse the intents whose chart types no dimension can currently satisfy (SPEC §6.1).

    Rejecting is mandatory rather than tidy. Both intents are in the plan schema, so from T09
    the model can emit them, and the schema is the model's action space: every intent in it
    needs a defined outcome — served, or refused with a reason. Falling through to the `table`
    branch would silently answer a different question.

    Keyed on `QUANTITATIVE_KEYS` rather than hardcoded, so adding `enrollment_count` lifts this
    in one place — and breaks T07's "never returned" sweep, which is how whoever adds it learns
    those two branches now need real coverage.
    """
    if QUANTITATIVE_KEYS:
        return []

    if plan.intent not in (Intent.SCATTER, Intent.HISTOGRAM):
        return []

    return [
        f"intent {plan.intent.value!r} requires a quantitative group_by dimension; none is "
        f"defined. Every dimension in this service is nominal, ordinal, or temporal "
        f"({_dimension_options()}). Enrollment is the only quantitative field and is computable "
        f"solely in complete_records mode. Use 'distribution' or 'trend' instead."
    ]


def _all_quotable_text(plan: AnalysisPlan, vocab: Vocabulary) -> list[str]:
    """Caller- and vocabulary-supplied strings the interpretation is allowed to echo.

    Needed because the digit rule below would otherwise reject honest prose: `COVID-19` and
    `Phase 2` both carry digits that are not years, and both are strings we put there.
    """
    text: list[str] = []
    filters = [plan.filters, *(series.filters for series in plan.series)]
    for filter_set in filters:
        for field_name, enum_name in ENUM_BY_FILTER_FIELD.items():
            raw = getattr(filter_set, field_name)
            values = raw if isinstance(raw, list) else ([] if raw is None else [raw])
            for value in values:
                text.extend((value, vocab.label(enum_name, value)))
        text.extend(
            value
            for value in (
                filter_set.condition,
                filter_set.intervention,
                filter_set.sponsor,
                filter_set.term,
                filter_set.country,
            )
            if value
        )
    text.extend(series.label for series in plan.series)
    return text


def _check_interpretation(plan: AnalysisPlan, vocab: Vocabulary) -> list[str]:
    errors: list[str] = []
    if not plan.interpretation.strip():
        errors.append("interpretation is empty; describe in one sentence what is being counted.")
        return errors

    if len(plan.interpretation) > 300:
        errors.append(f"interpretation is {len(plan.interpretation)} characters; the limit is 300.")

    residue = plan.interpretation
    for quotable in sorted(_all_quotable_text(plan, vocab), key=len, reverse=True):
        residue = re.sub(re.escape(quotable), " ", residue, flags=re.IGNORECASE)

    smuggled = [token for token in _NUMBER.findall(residue) if not _is_year(token)]
    if smuggled:
        errors.append(
            f"interpretation contains the number(s) {', '.join(smuggled)}, which are not years. "
            f"The interpretation describes what was counted; it must never state a count, "
            f"because the counts come from the API after planning."
        )
    return errors


def _is_year(token: str) -> bool:
    return token.isdigit() and len(token) == 4 and MIN_YEAR <= int(token) <= MAX_YEAR


def _check_metric_is_servable(plan: AnalysisPlan) -> list[str]:
    """BUILD-PLAN §6.3: enrollment metrics need record mode, which lands in T10.

    Flagged rather than silently degraded to `study_count` — answering a different question than
    the one asked is the failure this whole service is built to avoid. Remove when T10 lands.
    """
    if plan.metric is Metric.STUDY_COUNT:
        return []
    return [
        f"metric is {plan.metric.value!r}, which needs per-record enrollment values that the "
        f"count-based engine cannot produce yet. Use 'study_count'."
    ]


def validate_plan(plan: AnalysisPlan, vocab: Vocabulary) -> list[str]:
    """Every problem with `plan`, as sentences a model can act on. Empty list means valid.

    Returns messages rather than raising: T09 feeds them back for at most two repair attempts,
    then falls back to the heuristic planner (SPEC §3). A plan still invalid after all of that
    is the caller's answer, and it surfaces as `unplannable_query` — not `invalid_request`,
    since the request was well-formed and the question is what could not be served.
    """
    return [
        *_check_dimensions(plan),
        *_check_enums(plan, vocab),
        *_check_years(plan),
        *_check_coherence(plan),
        *_check_interpretation(plan, vocab),
        *_check_metric_is_servable(plan),
    ]


# Request field -> plan filter field. `sponsor` maps to lead sponsor only (SPEC §2.1).
HARD_CONSTRAINTS: dict[str, str] = {
    "drug_name": "intervention",
    "condition": "condition",
    "sponsor": "sponsor",
    "country": "country",
    "phase": "phase",
    "status": "status",
    "study_type": "study_type",
    "start_year": "start_year",
    "end_year": "end_year",
}


def enforce_hard_constraints(
    plan: AnalysisPlan, req: AnalyzeRequest
) -> tuple[AnalysisPlan, list[str]]:
    """Overwrite plan filters with the request's structured fields (SPEC §2.1).

    Structured fields are hard constraints: they override anything the planner inferred, and
    the model is not allowed to contradict them. Runs on every plan from every planner, so an
    LLM that ignores `drug_name` cannot change which studies are counted.

    `series[].filters` are left alone. A series overlay exists precisely to vary one field
    across series — a two-sponsor comparison would be flattened into one series if the
    request's `sponsor` were stamped onto both.

    Returns the plan and the overrides it made, for `meta.assumptions`.
    """
    overrides: dict[str, object] = {}
    assumptions: list[str] = []

    for request_field, filter_field in HARD_CONSTRAINTS.items():
        requested = getattr(req, request_field)
        if requested is None or (isinstance(requested, list) and not requested):
            continue

        current = getattr(plan.filters, filter_field)
        if current == requested:
            continue

        overrides[filter_field] = requested
        assumptions.append(
            f"filters.{filter_field} was set to {requested!r} from the request's "
            f"{request_field!r} field"
            + (f", replacing the planner's {current!r}" if current else "")
            + "."
        )

    if not overrides:
        return plan, []

    return plan.model_copy(update={"filters": plan.filters.model_copy(update=overrides)}), (
        assumptions
    )
