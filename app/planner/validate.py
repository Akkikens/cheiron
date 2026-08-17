"""Plan validation and hard-constraint enforcement. SPEC §3.

Every message here is read twice: once by a human debugging, and once by the model as repair
input (SPEC §3, T09). So each one names the offending field, the offending value, and something
valid to use instead. A message like "invalid group_by" is useless to both readers.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.engine.dimensions import QUANTITATIVE_KEYS, REGISTRY, TEMPORAL_KEYS, is_temporal
from app.errors import CheironError, ErrorCode
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

# A standalone number, not one glued into a name. `PD-1`, `COVID-19` and `SARS-CoV-2` are things
# the caller asked about; rejecting them as smuggled counts turned valid plans into 422s.
# `/` is a boundary character too, so `phase 1/2` reads as one label rather than as the number 2
# sitting next to the word "trials".
_STANDALONE = re.compile(r"(?<![\w/-])(\d{1,3}(?:,\d{3})+|\d+)(?![\w/-])")

# What makes a number a *count* rather than a quantity: it is counting the things this service
# counts. "the top 10 countries" is a plan detail; "10 trials" is a result the model invented.
_COUNTED_THING = re.compile(
    r"\s*(?:\w+\s+){0,2}?(trials?|stud(?:y|ies)|records?|results?|participants?|patients?"
    r"|enroll(?:ed|ment))\b",
    re.IGNORECASE,
)

# The other tell: a quantifier in front of it. "the top 5 sponsors" and "about 400" are both
# claims about the result, even though neither number touches the word "trials".
_QUANTIFIER = re.compile(
    r"\b(?:top|first|last|about|approximately|roughly|around|over|under|nearly|n\s*=)\s*$",
    re.IGNORECASE,
)


def _smuggled_counts(residue: str, allowed_years: set[str]) -> list[str]:
    """Numbers in `residue` that read as results rather than as part of a name.

    `residue` has already had every value the caller supplied stripped out, so what remains is
    the model's own prose. Three things are counts: thousands-separated numbers, runs of four or
    more digits that are not one of this plan's filter years, and any number directly describing
    a quantity of trials.
    """
    smuggled: list[str] = []
    for match in _STANDALONE.finditer(residue):
        token = match.group(1)
        if "," in token:
            smuggled.append(token)
            continue
        if token in allowed_years:
            continue
        if (
            len(token) >= 4
            or _COUNTED_THING.match(residue, match.end())
            or _QUANTIFIER.search(residue[: match.start()])
        ):
            smuggled.append(token)
    return smuggled


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

    # Only when a quantitative dimension exists: with none, `_check_intent_is_reachable` refuses
    # the intent outright and this would add a second message saying the same thing less well.
    for quantitative_intent in (Intent.SCATTER, Intent.HISTOGRAM) if QUANTITATIVE_KEYS else ():
        if plan.intent is quantitative_intent and plan.group_by.dimension not in QUANTITATIVE_KEYS:
            errors.append(
                f"intent is {quantitative_intent.value!r} but group_by.dimension is "
                f"{plan.group_by.dimension!r}, which is not quantitative. Use "
                f"{' or '.join(sorted(QUANTITATIVE_KEYS)) or 'a quantitative dimension'}, or "
                f"change the intent to 'distribution'."
            )

    if len(plan.series) == 1:
        errors.append(
            "series has exactly 1 entry; a single series is not a comparison and its filters "
            "would never be applied. Use 2-4 series, or move the filters into `filters` and "
            "drop `series`."
        )

    if len(plan.series) > 1 and plan.secondary_group_by is not None:
        errors.append(
            "series and secondary_group_by are both set; a grouped bar chart has one breakdown "
            "channel and cannot show two. Drop secondary_group_by, or drop series and ask for "
            "the cross-tab alone."
        )

    if plan.metric is not Metric.STUDY_COUNT and plan.secondary_group_by is not None:
        errors.append(
            f"metric is {plan.metric.value!r} with secondary_group_by set; a cross-tab counts "
            f"studies per cell, not participants. Drop secondary_group_by, or set metric to "
            f"'study_count'."
        )

    if len(plan.series) > 4:
        errors.append(
            f"series has {len(plan.series)} entries; this service renders at most 4. Drop "
            f"series beyond the four you care about, or ask one question per series."
        )

    # `scatter` deliberately does NOT require secondary_group_by. It plots one point per study
    # with enrollment against start date, so the second axis is a property of the chart rather
    # than a choice the plan makes: demanding a field the renderer ignores would invite a
    # caller to set it and expect it to matter.

    errors.extend(_check_intent_is_reachable(plan))
    return errors


def _check_intent_is_reachable(plan: AnalysisPlan) -> list[str]:
    """Refuse the intents whose chart types no dimension can currently satisfy (SPEC §6.1).

    Rejecting is mandatory rather than tidy. Both intents are in the plan schema, so from T09
    the model can emit them, and the schema is the model's action space: every intent in it
    needs a defined outcome: served, or refused with a reason. Falling through to the `table`
    branch would silently answer a different question.

    Keyed on `QUANTITATIVE_KEYS` rather than hardcoded, so adding `enrollment_count` lifts this
    in one place, and breaks T07's "never returned" sweep, which is how whoever adds it learns
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

    # Years are only allowed when they appear as filter bounds — otherwise "2024 trials" is a
    # year-shaped count the model invented.
    smuggled = _smuggled_counts(residue, _filter_years(plan))
    if smuggled:
        errors.append(
            f"interpretation contains the number(s) {', '.join(smuggled)}, which state a result "
            f"rather than name something the caller asked about. The interpretation describes "
            f"what will be counted; it must never state a count, because the counts come from "
            f"the API after planning. A year is allowed when it is one of this plan's own "
            f"filters.start_year or filters.end_year."
        )
    return errors


def _filter_years(plan: AnalysisPlan) -> set[str]:
    years: set[str] = set()
    for filters in (plan.filters, *(series.filters for series in plan.series)):
        if filters.start_year is not None:
            years.add(str(filters.start_year))
        if filters.end_year is not None:
            years.add(str(filters.end_year))
    return years


def _check_labels_for_smuggled_counts(plan: AnalysisPlan, vocab: Vocabulary) -> list[str]:
    """Series labels and free-text filters reach chart chrome; they must not invent counts."""
    errors: list[str] = []
    quotable = set(_all_quotable_text(plan, vocab))
    # Series labels are themselves quotable for the interpretation check; strip them so we
    # inspect each label in isolation rather than allowing it to excuse itself.
    for series in plan.series:
        residue = series.label
        for allowed in sorted((q for q in quotable if q != series.label), key=len, reverse=True):
            residue = re.sub(re.escape(allowed), " ", residue, flags=re.IGNORECASE)
        smuggled = _smuggled_counts(residue, _filter_years(plan))
        if smuggled:
            errors.append(
                f"series label {series.label!r} contains the number(s) {', '.join(smuggled)}. "
                f"Labels name a series; they must never state a count."
            )

    for scope, filters in (
        ("filters", plan.filters),
        *((f"series[{i}].filters", s.filters) for i, s in enumerate(plan.series)),
    ):
        for field_name in ("condition", "intervention", "sponsor", "term", "country"):
            raw = getattr(filters, field_name)
            if not raw:
                continue
            # A bare filter value is allowed to contain digits that are part of the name
            # (COVID-19). Parenthetical counts are not.
            if _NUMBER.search(raw) and (
                "(" in raw or " n=" in raw.casefold() or "n=" in raw.casefold()
            ):
                errors.append(
                    f"{scope}.{field_name} is {raw!r}, which looks like it embeds a count. "
                    f"Use the search string alone; counts come from the API after planning."
                )
    return errors


def validate_plan(plan: AnalysisPlan, vocab: Vocabulary) -> list[str]:
    """Every problem with `plan`, as sentences a model can act on. Empty list means valid.

    Returns messages rather than raising: T09 feeds them back for at most two repair attempts,
    then falls back to the heuristic planner (SPEC §3). A plan still invalid after all of that
    is the caller's answer, and it surfaces as `unplannable_query`: not `invalid_request`,
    since the request was well-formed and the question is what could not be served.

    Enrollment metrics are plan-valid (T10): above the record-mode threshold the engine refuses
    at run time with `unplannable_query` rather than silently degrading to `study_count`.
    """
    return [
        *_check_dimensions(plan),
        *_check_enums(plan, vocab),
        *_check_years(plan),
        *_check_coherence(plan),
        *_check_interpretation(plan, vocab),
        *_check_labels_for_smuggled_counts(plan, vocab),
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


def overlay_filters(base: StudyFilter, overlay: StudyFilter) -> StudyFilter:
    """AND hard-constraint filters into a series overlay. Overlay fields win when set.

    A comparison varies one field across series (usually sponsor). Everything else on
    `plan.filters`, especially request hard constraints like `drug_name`, must still apply, or
    `meta.filters_applied` and the upstream query disagree.

    Letting the overlay win is only safe because `enforce_hard_constraints` has already refused
    any plan whose series vary a field the request pinned. Without that guard, this line is how
    a caller's own filter gets silently overwritten.
    """
    merged = base.model_dump()
    for field_name in StudyFilter.model_fields:
        overlay_value = getattr(overlay, field_name)
        if field_name in ("phase", "status"):
            if overlay_value:
                merged[field_name] = overlay_value
        elif overlay_value is not None:
            merged[field_name] = overlay_value
    return StudyFilter.model_validate(merged)


def enforce_hard_constraints(
    plan: AnalysisPlan, req: AnalyzeRequest
) -> tuple[AnalysisPlan, list[str]]:
    """Overwrite plan filters with the request's structured fields (SPEC §2.1).

    Structured fields are hard constraints: they override anything the planner inferred, and
    the model is not allowed to contradict them. Runs on every plan from every planner, so an
    LLM that ignores `drug_name` cannot change which studies are counted.

    Series overlays are checked here too. At aggregate time each series is merged with
    `plan.filters` via `overlay_filters`, where an overlay value *wins*. So a request pinning
    `sponsor="Novartis"` alongside a comparison of Merck against Pfizer had each series quietly
    overriding the caller's own constraint: the numbers were Merck's and Pfizer's while
    `meta.filters_applied` reported Novartis. Stamping the pinned value over both overlays
    instead is no better, because it draws one number as two differently labelled bars. Both are
    fabrications, so a request that pins the field a comparison varies is refused outright.

    Returns the plan and the overrides it made, for `meta.assumptions`.
    """
    overrides: dict[str, object] = {}
    assumptions: list[str] = []
    pinned: dict[str, object] = {}

    for request_field, filter_field in HARD_CONSTRAINTS.items():
        requested = getattr(req, request_field)
        if requested is None or (isinstance(requested, list) and not requested):
            continue

        pinned[filter_field] = requested

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

    _reject_overlay_conflicts(plan, pinned)

    if not overrides:
        return plan, assumptions

    return plan.model_copy(update={"filters": plan.filters.model_copy(update=overrides)}), (
        assumptions
    )


def _reject_overlay_conflicts(plan: AnalysisPlan, pinned: dict[str, object]) -> None:
    """Refuse a comparison that varies a field the request has pinned.

    Fields the request did not pin are untouched: they still AND into every series, which is the
    whole point of `overlay_filters`.
    """
    conflicts: list[dict[str, object]] = []

    for index, series in enumerate(plan.series):
        for field_name, requested in pinned.items():
            overlay_value = getattr(series.filters, field_name)
            empty = overlay_value is None or (isinstance(overlay_value, list) and not overlay_value)
            if empty or overlay_value == requested:
                continue
            conflicts.append(
                {
                    "field": f"series[{index}].filters.{field_name}",
                    "value": overlay_value,
                    "message": (
                        f"series {series.label!r} filters {field_name} to {overlay_value!r}, but "
                        f"the request pins {field_name} to {requested!r}. A comparison cannot "
                        f"vary a field the request has fixed: drop the request's constraint to "
                        f"compare across it, or compare a different field."
                    ),
                }
            )

    if conflicts:
        raise CheironError(
            ErrorCode.UNPLANNABLE_QUERY,
            "The request's filters contradict the comparison it asks for.",
            details=conflicts,
        )
