# T04: Request, plan, and response models

**Est. 20 min · depends on: T01 · unblocks: T05, T07**

Pure Pydantic v2. No I/O, no business logic. Transcribe SPEC §2, §3, §4 exactly: where
this task and SPEC disagree, SPEC wins.

## `app/models/request.py` (SPEC §2)

`AnalyzeRequest` with `model_config = ConfigDict(extra="forbid")`: this is how §2.1's
"unknown top-level fields are **rejected** (422), not ignored" is satisfied. Same for
`Options`.

Fields and validation exactly per the §2.1 table: `query` required, 3–1000 chars **after
strip**; `drug_name`/`condition`/`sponsor` ≤200; `country` ≤100; `phase`/`status`/
`study_type` validated against the **live** vocabulary (not a hardcoded Literal: leave a
`validate_against(vocab)` method for the route to call, since Pydantic can't reach the
async loader); `start_year`/`end_year` 1900–2100 with a model validator for
`start_year <= end_year`.

`Options` defaults exactly per §2.2: `max_buckets=20` (1–100), `include_citations=True`,
`citations_per_datum=3` (0–10), `explain=False`.

## `app/models/plan.py` (SPEC §3)

```python
class Intent(StrEnum):   # distribution trend comparison geo network scatter histogram list
class Metric(StrEnum):   # study_count enrollment_sum enrollment_median
class ChartType(StrEnum) # the ten types in SPEC §6.2

class Bin(BaseModel):        size: int
class GroupBy(BaseModel):    dimension: str; bin: Bin | None = None
class StudyFilter(BaseModel):
    condition: str | None; intervention: str | None; sponsor: str | None
    term: str | None; country: str | None
    phase: list[str]; status: list[str]; study_type: str | None
    start_year: int | None; end_year: int | None
class SeriesSpec(BaseModel): label: str; filters: StudyFilter
class AnalysisPlan(BaseModel):
    intent: Intent
    filters: StudyFilter
    series: list[SeriesSpec] = []
    group_by: GroupBy
    secondary_group_by: GroupBy | None = None
    metric: Metric = Metric.STUDY_COUNT
    viz_hint: ChartType | None = None       # advisory only, SPEC §3
    interpretation: str                      # <= 300 chars
```

Two required helpers:

- `AnalysisPlan.json_schema_strict()` → the OpenAI Structured Outputs schema: every field
  `required`, `additionalProperties: false`, nullable via `["string", "null"]` unions.
  T09 consumes this; build it now so the model shape and the schema can never drift.
- `AnalysisPlan.normalized_key()` → stable cache key (SPEC §7): sorted lists,
  case-folded strings, `interpretation` and `viz_hint` **excluded** (they don't affect
  which numbers come back).

## `app/models/response.py` (SPEC §4)

`Citation`, `Channel` (`{field, type, label, sort?, format?, scale?}` where `type` ∈
`nominal|ordinal|quantitative|temporal|geo`), `Visualization`
(`{type, title, subtitle?, encoding, data, annotations?}`), `Coverage`, `Provenance`,
`TimingMs`, `Meta`, `AnalyzeResponse`.

`Coverage` per §4.3 with **every** field present on every response:
`aggregation_mode`, `groupby_semantics`, `bucket_sum`, `unclassified_count`,
`overlap_note` (nullable), `sample_size` (nullable), `sample_coverage` (nullable).

`data` rows are `list[dict[str, Any]]`: the key set is chart-type dependent (flat rows,
or `{nodes, edges}` for `network_graph`), so don't over-model it. Do add a validator
asserting every `encoding` channel's `field` exists in **every** `data` row (SPEC §4.1:
"`field` always names a key present in every `data` row"). For `network_graph`, skip that
check and instead assert `data` has exactly `nodes` and `edges`.

Forbid a `share_of_total` / `percentage` / `share` key in any `data` row when
`coverage.groupby_semantics == "overlapping"`. SPEC §6.1 makes this non-overridable, so
enforce it in the type system rather than trusting the renderer.

## Tests

- Unknown top-level field → `ValidationError` (→ 422 `invalid_request`, `details[]` names
  the field).
- `query="  ab  "` → rejected (2 chars after strip); `query=" " * 50` → rejected.
- `start_year=2020, end_year=2015` → rejected.
- `citations_per_datum=11`, `max_buckets=0` → rejected.
- `normalized_key()` is identical for two plans differing only in `interpretation` /
  `viz_hint`, and different when a filter differs.
- `json_schema_strict()` round-trips: a plan → schema-validated JSON → parses back equal.
- A `Visualization` whose `encoding.x.field` is absent from one `data` row → rejected.
- An overlapping-semantics response carrying `share_of_total` → rejected.

## Guardrails

No planner, no engine, no chart selection. Models only.
