"""`POST /analyze` response models. SPEC §4.

Two SPEC rules are enforced here rather than trusted to the renderer, because both fail
*plausibly* when broken — a chart still draws, it just lies:

- every `encoding` channel's `field` must exist in every `data` row (SPEC §4.1);
- no share/percentage key may appear when `groupby_semantics` is `overlapping`, since the
  denominator does not exist (SPEC §6.1, A1).
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.plan import AnalysisPlan, ChartType

ChannelType = Literal["nominal", "ordinal", "quantitative", "temporal", "geo"]
AggregationMode = Literal["complete_records", "server_counts", "sampled_then_confirmed"]
GroupBySemantics = Literal["partition", "overlapping"]
PlannerName = Literal["llm", "llm_repaired", "heuristic_fallback"]

SHARE_KEYS = frozenset({"share_of_total", "share", "percentage", "percent", "pct"})
"""Forbidden under overlapping semantics: bucket memberships have no meaningful whole."""

NETWORK_DATA_KEYS = frozenset({"nodes", "edges"})


class Citation(BaseModel):
    """SPEC §4.2. `excerpt` is a verbatim serialization of upstream, never paraphrased."""

    model_config = ConfigDict(extra="forbid")

    nct_id: str
    field: str
    excerpt: str
    url: str


class Channel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    type: ChannelType
    label: str
    sort: list[str] | None = None
    format: str | None = None
    scale: str | None = None
    unit: str | None = None
    """What the numbers count, for an axis or a KPI: "studies" or "participants".

    SPEC §6.2 gives `kpi` a `unit`; the brief asks for whatever a frontend needs to render
    appropriately. "1,750" on a bar chart is ambiguous between trials and people, and the two
    metrics in this service mean exactly those two different things.
    """
    bin_start: str | None = None
    bin_end: str | None = None
    """SPEC §6.2: a histogram's x channel names the row keys holding each bar's range.

    A histogram bar spans an interval, so a renderer needs both edges — `field` alone gives it a
    label to print, not a width to draw."""


class Visualization(BaseModel):
    """SPEC §4.1.

    `encoding` and `data` are intentionally loose containers. Channels vary by chart type
    (§6.2) — `table` carries `columns[]`, `network_graph` carries node/edge descriptions rather
    than channels — so the shape is validated per type below instead of being forced into one
    static type.
    """

    model_config = ConfigDict(extra="forbid")

    type: ChartType
    title: str
    subtitle: str | None = None
    encoding: dict[str, Any]
    data: list[dict[str, Any]] | dict[str, Any]
    annotations: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _encoding_matches_data(self) -> Self:
        if self.type is ChartType.NETWORK_GRAPH:
            return self._validate_network()

        if not isinstance(self.data, list):
            raise ValueError(f"{self.type} requires `data` to be a list of rows")

        for name, channel in self._channels():
            for index, row in enumerate(self.data):
                if channel.field not in row:
                    raise ValueError(
                        f"encoding.{name}.field {channel.field!r} is missing from data row "
                        f"{index}; SPEC §4.1 requires it in every row"
                    )
        return self

    def _validate_network(self) -> Self:
        """SPEC §4.1: `network_graph` carries `{nodes, edges}` instead of flat rows."""
        if not isinstance(self.data, dict) or set(self.data) != NETWORK_DATA_KEYS:
            raise ValueError("network_graph `data` must be exactly {nodes, edges}")
        if set(self.encoding) != NETWORK_DATA_KEYS:
            raise ValueError("network_graph `encoding` must be exactly {nodes, edges}")
        return self

    def _channels(self) -> list[tuple[str, Channel]]:
        """Parse the channel-shaped entries. `table`'s `columns` is a list of them."""
        parsed: list[tuple[str, Channel]] = []
        for name, spec in self.encoding.items():
            if isinstance(spec, list):
                parsed.extend(
                    (f"{name}[{index}]", Channel.model_validate(item))
                    for index, item in enumerate(spec)
                )
            else:
                parsed.append((name, Channel.model_validate(spec)))
        return parsed


class Coverage(BaseModel):
    """SPEC §4.3. Every field is required on every response — nullable, but never absent.

    A bare `truncated: true` is not acceptable anywhere in this API, so the nullable fields are
    explicit `None` rather than omitted: the caller can tell "not sampled" from "forgot to say".
    """

    model_config = ConfigDict(extra="forbid")

    aggregation_mode: AggregationMode
    groupby_semantics: GroupBySemantics
    bucket_sum: int = Field(ge=0)
    unclassified_count: int = Field(ge=0)
    overlap_note: str | None
    sample_size: int | None
    sample_coverage: float | None = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _overlapping_states_its_numbers(self) -> Self:
        if self.groupby_semantics == "overlapping" and not self.overlap_note:
            raise ValueError(
                "overlapping semantics requires overlap_note to state the actual numbers "
                "(SPEC §4.3)"
            )
        return self

    @model_validator(mode="after")
    def _sampling_is_disclosed_in_full(self) -> Self:
        sampled = self.aggregation_mode == "sampled_then_confirmed"
        if sampled and (self.sample_size is None or self.sample_coverage is None):
            raise ValueError(
                "sampled_then_confirmed requires sample_size and sample_coverage (SPEC §4.3)"
            )
        return self


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "clinicaltrials.gov"
    api_version: str
    data_timestamp: str
    retrieved_at: str


class TimingMs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: int = Field(ge=0)
    retrieve: int = Field(ge=0)
    total: int = Field(ge=0)


class Meta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation: str
    planner: PlannerName
    filters_applied: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    total_matching_studies: int = Field(ge=0)
    coverage: Coverage
    provenance: Provenance
    timing_ms: TimingMs
    # Both only when options.explain (SPEC §4.3).
    api_query_log: list[str] | None = None
    plan: AnalysisPlan | None = None


class AnalyzeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visualization: Visualization
    meta: Meta

    @model_validator(mode="after")
    def _no_share_under_overlapping_semantics(self) -> Self:
        """SPEC §6.1 makes this non-overridable, so it is a type error, not a review comment."""
        if self.meta.coverage.groupby_semantics != "overlapping":
            return self

        rows = self.visualization.data
        candidates = rows if isinstance(rows, list) else [rows]
        for index, row in enumerate(candidates):
            offending = SHARE_KEYS.intersection(row)
            if offending:
                raise ValueError(
                    f"data row {index} carries {sorted(offending)} while groupby_semantics is "
                    "overlapping; bucket memberships have no denominator (SPEC §6.1)"
                )
        return self
