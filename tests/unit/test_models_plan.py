"""SPEC §3: the IR, its strict schema, and its cache key."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.plan import (
    AnalysisPlan,
    Bin,
    ChartType,
    GroupBy,
    Intent,
    Metric,
    SeriesSpec,
    StudyFilter,
    _strictify,
)

# SPEC §3 and §6.2, transcribed by hand.
SPEC_INTENTS = {
    "distribution",
    "trend",
    "comparison",
    "geo",
    "network",
    "scatter",
    "histogram",
    "list",
}
SPEC_METRICS = {"study_count", "enrollment_sum", "enrollment_median"}
SPEC_CHART_TYPES = {
    "bar_chart",
    "grouped_bar_chart",
    "stacked_bar_chart",
    "time_series",
    "scatter_plot",
    "histogram",
    "choropleth_map",
    "network_graph",
    "table",
    "kpi",
}


def a_plan(**overrides: Any) -> AnalysisPlan:
    base: dict[str, Any] = {
        "intent": Intent.DISTRIBUTION,
        "filters": StudyFilter(intervention="Pembrolizumab"),
        "group_by": GroupBy(dimension="phase"),
        "interpretation": "Distribution of pembrolizumab trials across phases.",
    }
    return AnalysisPlan(**{**base, **overrides})


def test_enums_match_spec() -> None:
    assert {member.value for member in Intent} == SPEC_INTENTS
    assert {member.value for member in Metric} == SPEC_METRICS
    assert {member.value for member in ChartType} == SPEC_CHART_TYPES


def test_spec_example_plan_parses() -> None:
    """SPEC §3's worked example, verbatim."""
    plan = AnalysisPlan.model_validate(
        {
            "intent": "trend",
            "filters": {
                "condition": None,
                "intervention": "Pembrolizumab",
                "sponsor": None,
                "term": None,
                "country": None,
                "phase": [],
                "status": [],
                "study_type": None,
                "start_year": 2015,
                "end_year": 2025,
            },
            "series": [],
            "group_by": {"dimension": "start_year", "bin": {"size": 1}},
            "secondary_group_by": None,
            "metric": "study_count",
            "viz_hint": "time_series",
            "interpretation": (
                "Annual count of interventional trials studying pembrolizumab, 2015-2025."
            ),
        }
    )

    assert plan.intent is Intent.TREND
    assert plan.group_by.bin == Bin(size=1)
    assert plan.metric is Metric.STUDY_COUNT


def test_default_metric_is_study_count() -> None:
    assert a_plan().metric is Metric.STUDY_COUNT


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        a_plan(chart_type="bar_chart")


def test_interpretation_is_capped() -> None:
    with pytest.raises(ValidationError):
        a_plan(interpretation="x" * 301)


def test_multiple_series_requires_comparison_intent() -> None:
    """SPEC §3: length > 1 ⇒ comparison."""
    series = [
        SeriesSpec(label="Merck", filters=StudyFilter(sponsor="Merck Sharp & Dohme LLC")),
        SeriesSpec(label="Pfizer", filters=StudyFilter(sponsor="Pfizer")),
    ]
    with pytest.raises(ValidationError, match="requires intent=comparison"):
        a_plan(series=series)

    assert len(a_plan(intent=Intent.COMPARISON, series=series).series) == 2


# --- viz_hint is advisory (SPEC §3, A7) ---------------------------------------------------


@pytest.mark.parametrize("hint", ["pie_chart", "donut_chart", "PIE_CHART"])
def test_unrenderable_hint_is_discarded_not_fatal(hint: str) -> None:
    """A7: a pie_chart hint is discarded. The request must not fail because of advice."""
    plan = a_plan(viz_hint=hint)

    assert plan.viz_hint is None
    assert plan.discarded_viz_hint == hint.lower()


def test_a_typo_in_viz_hint_still_fails_loudly() -> None:
    with pytest.raises(ValidationError):
        a_plan(viz_hint="bar_chrat")


def test_a_valid_hint_is_kept() -> None:
    plan = a_plan(viz_hint="bar_chart")

    assert plan.viz_hint is ChartType.BAR_CHART
    assert plan.discarded_viz_hint is None


# --- normalized_key (SPEC §7) -------------------------------------------------------------


def test_key_ignores_interpretation() -> None:
    """Prose changes no number, so a rephrasing is the same cached result."""
    first = a_plan(interpretation="One phrasing.")
    second = a_plan(interpretation="A completely different phrasing.")

    assert first.normalized_key() == second.normalized_key()


def test_key_includes_viz_hint_because_it_can_change_the_chart() -> None:
    """Advisory is not the same as inert.

    The registry consults `viz_hint` to break ties (bar/table/kpi, grouped/stacked), so two
    requests differing only in the hint can legitimately get different chart types. Sharing a
    cache entry would serve the first caller's chart to the second.
    """
    as_bar = a_plan(viz_hint="bar_chart")
    as_table = a_plan(viz_hint="table")

    assert as_bar.normalized_key() != as_table.normalized_key()


def test_key_ignores_discarded_viz_hint() -> None:
    """A server-side annotation on the plan, not model output: same treatment as viz_hint."""
    with_discard = a_plan(viz_hint="pie_chart")
    without = a_plan()

    assert with_discard.discarded_viz_hint == "pie_chart"
    assert without.discarded_viz_hint is None
    # An unrenderable hint is discarded before it reaches the registry, so unlike a live hint it
    # cannot change the chart, and must not split the cache.
    assert with_discard.normalized_key() == without.normalized_key()


def test_key_changes_when_a_filter_changes() -> None:
    assert (
        a_plan(filters=StudyFilter(intervention="Pembrolizumab")).normalized_key()
        != a_plan(filters=StudyFilter(intervention="Nivolumab")).normalized_key()
    )


def test_key_is_case_insensitive_on_filter_text() -> None:
    assert (
        a_plan(filters=StudyFilter(intervention="PEMBROLIZUMAB")).normalized_key()
        == a_plan(filters=StudyFilter(intervention="pembrolizumab")).normalized_key()
    )


def test_key_ignores_list_order() -> None:
    assert (
        a_plan(filters=StudyFilter(phase=["PHASE3", "PHASE2"])).normalized_key()
        == a_plan(filters=StudyFilter(phase=["PHASE2", "PHASE3"])).normalized_key()
    )


def test_key_distinguishes_group_by() -> None:
    assert (
        a_plan(group_by=GroupBy(dimension="phase")).normalized_key()
        != a_plan(group_by=GroupBy(dimension="overall_status")).normalized_key()
    )


def test_key_distinguishes_metric() -> None:
    assert (
        a_plan(metric=Metric.STUDY_COUNT).normalized_key()
        != a_plan(metric=Metric.ENROLLMENT_SUM).normalized_key()
    )


def test_key_is_stable_across_calls() -> None:
    plan = a_plan()
    assert plan.normalized_key() == plan.normalized_key()
    assert len(plan.normalized_key()) == 64


# --- json_schema_strict (SPEC §3, consumed by T09) ----------------------------------------


def objects_in(schema: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


def test_every_object_forbids_extra_properties() -> None:
    for node in objects_in(AnalysisPlan.json_schema_strict()):
        assert node["additionalProperties"] is False


def test_the_transform_supplies_additional_properties_itself() -> None:
    """The assertion above passes on Pydantic's output alone, because every plan model sets
    `extra="forbid"`. This pins the transform, so a future model missing that config still
    produces a strict-legal schema instead of a silently permissive one.
    """
    permissive = {
        "type": "object",
        "properties": {"dimension": {"type": "string"}, "bin": {"type": "integer"}},
        "required": ["dimension"],
    }

    strict = _strictify(permissive)

    assert strict["additionalProperties"] is False
    assert strict["required"] == ["dimension", "bin"]


def test_every_property_is_required() -> None:
    """OpenAI strict mode has no notion of an optional key; nullability carries it."""
    for node in objects_in(AnalysisPlan.json_schema_strict()):
        assert set(node["required"]) == set(node.get("properties", {}))


def test_no_defaults_survive() -> None:
    assert "default" not in json.dumps(AnalysisPlan.json_schema_strict())


def test_schema_carries_no_docstring_prose() -> None:
    """Docstrings here are maintainer-facing; one names a private constant.

    If they reached the schema, editing a comment would change what the model sees.
    """
    serialized = json.dumps(AnalysisPlan.json_schema_strict())

    assert "title" not in serialized
    assert "description" not in serialized
    assert "_UNRENDERABLE_HINTS" not in serialized


def test_discarded_viz_hint_is_absent_from_the_published_schema() -> None:
    """Server-side annotation only. Under strict mode every property is required, so shipping
    this field would oblige the model to invent a value for something only the server writes.
    """
    schema = AnalysisPlan.json_schema_strict()

    assert "discarded_viz_hint" not in schema.get("properties", {})
    assert "discarded_viz_hint" not in schema.get("required", [])
    assert "discarded_viz_hint" not in json.dumps(schema)


# --- the Structured Outputs constraint set -------------------------------------------------
#
# T09's first live call should confirm this schema, not discover it. Everything OpenAI
# documents as a hard requirement for `strict: true` is asserted below, so a failure there
# points at the key or the model id rather than the schema.
# https://developers.openai.com/api/docs/guides/structured-outputs

STRICT_KEYWORDS = {
    "$defs",
    "$ref",
    "additionalProperties",
    "anyOf",
    "enum",
    "const",
    "items",
    "properties",
    "required",
    "type",
    "multipleOf",
    "maximum",
    "exclusiveMaximum",
    "minimum",
    "exclusiveMinimum",
    "minItems",
    "maxItems",
}
"""Structural keywords plus the documented-supported numeric and array constraints.

`minLength`/`maxLength` are excluded because the docs list only `pattern` and `format` for
strings, and an unsupported keyword under `strict: true` is documented to error. `pattern` and
`format` are excluded too, despite being supported: nothing here needs them, so their arrival
should be a deliberate choice re-checked against the docs rather than something a new
`Field(pattern=...)` slips in silently.
"""


def keywords_in(schema: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def walk(node: Any, inside_properties: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if not inside_properties:
                    found.add(key)
                walk(value, inside_properties=key in {"properties", "$defs"})
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


def test_schema_uses_only_documented_strict_keywords() -> None:
    """A keyword outside the subset is a 400 at request time, five tasks from here."""
    assert keywords_in(AnalysisPlan.json_schema_strict()) <= STRICT_KEYWORDS


@pytest.mark.parametrize(
    "keyword", ["nullable", "oneOf", "allOf", "not", "if", "then", "else", "patternProperties"]
)
def test_schema_avoids_keywords_strict_mode_rejects(keyword: str) -> None:
    """`nullable` is the OpenAPI spelling; strict mode wants a `["string", "null"]` union.

    The rest are the composition keywords the docs list as not yet supported.
    """
    assert f'"{keyword}"' not in json.dumps(AnalysisPlan.json_schema_strict())


def test_root_is_an_object_and_not_a_union() -> None:
    """Documented explicitly: the root must be an object and must not use anyOf."""
    schema = AnalysisPlan.json_schema_strict()

    assert schema["type"] == "object"
    assert "anyOf" not in schema


def test_length_caps_are_enforced_by_the_model_not_the_schema() -> None:
    """`maxLength` is dropped from the schema, so parsing has to be what holds the line."""
    assert "maxLength" not in json.dumps(AnalysisPlan.json_schema_strict())
    with pytest.raises(ValidationError):
        a_plan(interpretation="x" * 301)


def test_schema_respects_openai_nesting_and_property_limits() -> None:
    """Documented ceilings: 5,000 object properties total, up to 10 levels of nesting."""
    schema = AnalysisPlan.json_schema_strict()
    depth = _nesting_depth(schema, schema.get("$defs", {}))

    assert sum(len(node.get("properties", {})) for node in objects_in(schema)) <= 5_000
    assert depth <= 10
    assert depth > 1, f"depth {depth} is implausible; the walker is probably not walking"


def test_schema_respects_openai_string_and_enum_budgets() -> None:
    """Documented ceilings: 1,000 enum values, and 120,000 characters of names and values.

    Both are generous relative to this schema. They are asserted anyway because `Metric` and
    `ChartType` are the kind of enum a later task grows, and the failure mode upstream is a 400
    rather than a truncation.
    """
    schema = AnalysisPlan.json_schema_strict()
    enum_values = [
        value for definition in schema["$defs"].values() for value in definition.get("enum", [])
    ]
    property_names = [name for node in objects_in(schema) for name in node.get("properties", {})]
    definition_names = list(schema["$defs"])

    assert len(enum_values) <= 1_000
    assert sum(map(len, enum_values + property_names + definition_names)) <= 120_000


def _nesting_depth(node: Any, defs: dict[str, Any], seen: frozenset[str] = frozenset()) -> int:
    """Depth in objects, following `$ref` the way the API's own validator would."""
    if isinstance(node, list):
        return max((_nesting_depth(item, defs, seen) for item in node), default=0)
    if not isinstance(node, dict):
        return 0

    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        if name in seen:
            return 0
        return _nesting_depth(defs[name], defs, seen | {name})

    children = [value for key, value in node.items() if key != "$defs"]
    deepest = max((_nesting_depth(child, defs, seen) for child in children), default=0)
    return deepest + 1 if "properties" in node else deepest


def test_nullable_scalars_become_type_unions() -> None:
    schema = AnalysisPlan.json_schema_strict()
    condition = schema["$defs"]["StudyFilter"]["properties"]["condition"]

    assert condition["type"] == ["string", "null"]
    assert "anyOf" not in condition


def test_nullable_refs_stay_anyof() -> None:
    """A `$ref | null` union cannot collapse into a type array; strict mode allows anyOf."""
    secondary = AnalysisPlan.json_schema_strict()["properties"]["secondary_group_by"]

    assert "anyOf" in secondary
    assert any("$ref" in branch for branch in secondary["anyOf"])


def assert_required_keys_present(node: Any, payload: Any, defs: dict[str, Any], path: str) -> None:
    """Walk schema and payload together, checking the strict `required` contract at every depth.

    The drift this guards is narrow and specific: strict mode requires *every* property, so a
    field that Pydantic omits when serializing (a default, an unset optional) makes the model's
    output shape and ours disagree: at any nesting level, not just the root.
    """
    if isinstance(node, dict) and "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        assert_required_keys_present(defs[name], payload, defs, path)
        return

    if isinstance(node, dict) and "anyOf" in node:
        if payload is None:
            types = [branch.get("type") for branch in node["anyOf"]]
            assert "null" in types, f"{path} is null but the schema has no null branch"
            return
        for branch in node["anyOf"]:
            if branch.get("type") != "null":
                assert_required_keys_present(branch, payload, defs, path)
        return

    if isinstance(node, dict) and "properties" in node:
        assert isinstance(payload, dict), f"{path} should be an object"
        missing = set(node["required"]) - set(payload)
        assert not missing, f"strict schema requires {sorted(missing)} at {path}, dump omits them"
        for key, child in node["properties"].items():
            assert_required_keys_present(child, payload[key], defs, f"{path}.{key}")
        return

    if isinstance(node, dict) and node.get("type") == "array":
        assert isinstance(payload, list), f"{path} should be an array"
        for index, item in enumerate(payload):
            assert_required_keys_present(node["items"], item, defs, f"{path}[{index}]")


def test_schema_round_trips_a_plan() -> None:
    """A plan that exercises every nested object: series, bins, and a secondary group-by."""
    plan = a_plan(
        intent=Intent.COMPARISON,
        viz_hint="grouped_bar_chart",
        series=[
            SeriesSpec(label="Merck", filters=StudyFilter(sponsor="Merck Sharp & Dohme LLC")),
            SeriesSpec(label="Pfizer", filters=StudyFilter(sponsor="Pfizer", phase=["PHASE3"])),
        ],
        group_by=GroupBy(dimension="start_year", bin=Bin(size=1)),
        secondary_group_by=GroupBy(dimension="phase"),
    )
    schema = AnalysisPlan.json_schema_strict()
    payload = json.loads(plan.model_dump_json())

    assert_required_keys_present(schema, payload, schema["$defs"], "plan")
    assert AnalysisPlan.model_validate(payload) == plan


def test_the_round_trip_check_can_fail() -> None:
    """The walker above is only worth having if a missing key actually trips it."""
    schema = AnalysisPlan.json_schema_strict()
    payload = json.loads(a_plan().model_dump_json())
    del payload["filters"]["condition"]

    with pytest.raises(AssertionError, match="condition"):
        assert_required_keys_present(schema, payload, schema["$defs"], "plan")


def test_schema_exposes_the_closed_enums() -> None:
    schema = AnalysisPlan.json_schema_strict()
    assert set(schema["$defs"]["Intent"]["enum"]) == SPEC_INTENTS
    assert set(schema["$defs"]["Metric"]["enum"]) == SPEC_METRICS


def test_schema_has_nowhere_to_put_a_number() -> None:
    """SPEC §1: the model cannot emit a count because the IR has no field for one."""
    schema = json.dumps(AnalysisPlan.json_schema_strict())
    for forbidden in ("count", "total", "value", "data", "bucket"):
        assert f'"{forbidden}"' not in schema
