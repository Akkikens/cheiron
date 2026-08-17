"""`AnalysisPlan` — the only thing the model ever produces. SPEC §3.

Every field is a closed enum or a search string, which is what makes SPEC §1's thesis
enforceable: the model cannot emit a number because there is nowhere in this shape to put one.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema


class Intent(StrEnum):
    DISTRIBUTION = "distribution"
    TREND = "trend"
    COMPARISON = "comparison"
    GEO = "geo"
    NETWORK = "network"
    SCATTER = "scatter"
    HISTOGRAM = "histogram"
    LIST = "list"


class Metric(StrEnum):
    STUDY_COUNT = "study_count"
    ENROLLMENT_SUM = "enrollment_sum"
    ENROLLMENT_MEDIAN = "enrollment_median"


class ChartType(StrEnum):
    """The ten renderable types in SPEC §6.2 — and only those.

    Notably absent: `pie_chart`. It appears in SPEC §6.1 only as something the safety rules
    forbid, and in A7 only as a `viz_hint` that must be *discarded*, so it is never an output.
    `AnalysisPlan.viz_hint` therefore drops the known-unrenderable hints rather than rejecting
    the whole plan — see `_UNRENDERABLE_HINTS`.
    """

    BAR_CHART = "bar_chart"
    GROUPED_BAR_CHART = "grouped_bar_chart"
    STACKED_BAR_CHART = "stacked_bar_chart"
    TIME_SERIES = "time_series"
    SCATTER_PLOT = "scatter_plot"
    HISTOGRAM = "histogram"
    CHOROPLETH_MAP = "choropleth_map"
    NETWORK_GRAPH = "network_graph"
    TABLE = "table"
    KPI = "kpi"


_UNRENDERABLE_HINTS = frozenset(
    {
        "pie_chart",
        "pie",
        "donut_chart",
        "donut",
        "hundred_percent_stacked_bar_chart",
        "percent_stacked_bar_chart",
    }
)
"""Hints SPEC §6.1 makes non-selectable. Dropped on parse; a typo still fails loudly."""


class Bin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: int = Field(ge=1)


class GroupBy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    bin: Bin | None = None


class StudyFilter(BaseModel):
    """Same shape as SPEC §2.1's structured fields, so a request maps onto it field-for-field."""

    model_config = ConfigDict(extra="forbid")

    condition: str | None = None
    intervention: str | None = None
    sponsor: str | None = None
    term: str | None = None
    country: str | None = None
    phase: list[str] = Field(default_factory=list)
    status: list[str] = Field(default_factory=list)
    study_type: str | None = None
    start_year: int | None = None
    end_year: int | None = None


class SeriesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    filters: StudyFilter


class AnalysisPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    filters: StudyFilter
    series: list[SeriesSpec] = Field(default_factory=list)
    group_by: GroupBy
    secondary_group_by: GroupBy | None = None
    metric: Metric = Metric.STUDY_COUNT
    viz_hint: ChartType | None = None
    interpretation: str = Field(max_length=300)
    # Not part of the IR the model emits — only records that a forbidden hint arrived so the
    # registry can put the discard in `meta.warnings` (SPEC A7) rather than dropping it silently.
    discarded_viz_hint: SkipJsonSchema[str | None] = None

    @model_validator(mode="before")
    @classmethod
    def _capture_unrenderable_hint(cls, value: Any) -> Any:
        """`viz_hint` is advisory (SPEC §3); an unrenderable one is discarded, not fatal."""
        if not isinstance(value, dict):
            return value
        hint = value.get("viz_hint")
        if isinstance(hint, str) and hint.lower() in _UNRENDERABLE_HINTS:
            return {**value, "viz_hint": None, "discarded_viz_hint": hint.lower()}
        return value

    @model_validator(mode="after")
    def _series_implies_comparison(self) -> Self:
        if len(self.series) > 1 and self.intent is not Intent.COMPARISON:
            raise ValueError(
                f"{len(self.series)} series requires intent=comparison, got {self.intent}"
            )
        return self

    # --- helpers -------------------------------------------------------------------------

    @classmethod
    def json_schema_strict(cls) -> dict[str, Any]:
        """The OpenAI Structured Outputs schema (`strict: true`).

        Built from this model rather than hand-written so the prompt schema and the parsed
        shape cannot drift. T09 consumes it.
        """
        return _strictify(cls.model_json_schema())

    def normalized_key(self) -> str:
        """Stable cache key (SPEC §7).

        `interpretation` is excluded: it is prose and changes no number.

        `viz_hint` is **included**, despite being advisory. The registry consults it to break
        ties (bar/table/kpi, grouped/stacked), so two requests differing only in the hint can
        legitimately get different chart types — and sharing a cache entry would serve the first
        caller's chart to the second. Advice that is sometimes taken is part of the request.
        """
        canonical = _canonicalise(
            self.model_dump(mode="json", exclude={"interpretation", "discarded_viz_hint"})
        )
        return hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def _canonicalise(value: Any) -> Any:
    """Case-fold strings and sort lists so equivalent plans hash identically."""
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, dict):
        return {key: _canonicalise(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        folded = [_canonicalise(item) for item in value]
        return sorted(folded, key=lambda item: json.dumps(item, sort_keys=True))
    return value


_DROPPED_KEYWORDS = (
    "default",
    "title",
    "description",
    # OpenAI's documented supported `string` properties are `pattern` and `format` only, so a
    # length cap is at best unenforced and at worst a 400. `interpretation`'s 300-character
    # limit is enforced by this model at parse time instead, which is the only place it can be
    # a guarantee rather than a hint.
    "maxLength",
    "minLength",
)


def _strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a Pydantic JSON Schema into the strict subset OpenAI accepts.

    Three rules: every property is `required`, no object allows extras, and optionality is
    expressed as a nullable type rather than an absent key.

    `title` and `description` are dropped so the schema carries structure only. Pydantic
    derives them from docstrings, and docstrings here are written for maintainers — one of them
    names a private constant. Keeping them would mean an editorial change to a comment could
    alter model output with no test able to see it. Wording the model reads belongs in T09's
    prompt.
    """
    for definition in schema.get("$defs", {}).values():
        _strictify_node(definition)
    _strictify_node(schema)
    return schema


def _strictify_node(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _strictify_node(item)
        return
    if not isinstance(node, dict):
        return

    for noise in _DROPPED_KEYWORDS:
        node.pop(noise, None)

    if node.get("type") == "object" or "properties" in node:
        properties = node.get("properties", {})
        node["additionalProperties"] = False
        node["required"] = list(properties)

    _collapse_nullable(node)

    for key, child in list(node.items()):
        if key in {"properties", "$defs"} and isinstance(child, dict):
            for grandchild in child.values():
                _strictify_node(grandchild)
        elif key in {"anyOf", "oneOf", "items", "prefixItems"}:
            _strictify_node(child)


def _collapse_nullable(node: dict[str, Any]) -> None:
    """`anyOf: [{type: string}, {type: null}]` -> `type: ["string", "null"]`.

    Only collapses when every branch is a plain typed scalar; a `$ref | null` union has to stay
    an `anyOf`, which strict mode allows.
    """
    branches = node.get("anyOf")
    if not isinstance(branches, list) or len(branches) < 2:
        return
    if not all(isinstance(branch, dict) and set(branch) == {"type"} for branch in branches):
        return

    types = [branch["type"] for branch in branches]
    if "null" not in types:
        return

    node.pop("anyOf")
    node["type"] = types
