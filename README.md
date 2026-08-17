# Cheiron

A natural-language question about clinical trials in; a **renderable visualization
specification**, with every number traceable to ClinicalTrials.gov, out.

The design rests on one rule:

> **The language model never emits a number, a label, or a fact.
> It emits only a validated query plan. Everything downstream is deterministic.**

The model's entire job is translating a question into an `AnalysisPlan` whose every field is a
closed enum or a search string. It never sees study data, so it cannot invent a count.
Retrieval, aggregation, chart selection, and citation are all deterministic code operating on
exact API responses. `tests/unit/test_llm_planner.py::test_no_study_data_reaches_the_model`
asserts this holds in the implementation rather than only in the prose.

The full contract is [`SPEC.md`](SPEC.md). The verified upstream behaviour it depends on —
every claim measured with live calls — is [`docs/CTG-API-NOTES.md`](docs/CTG-API-NOTES.md).

---

## 1. Run it

```bash
uv sync --extra dev                      # or: pip install -e '.[dev]'
cp .env.example .env                     # add OPENAI_API_KEY for natural-language questions
uv run uvicorn app.main:create_app --factory --reload
```

Or without a local Python at all — convenience only, and the one instruction here that is
**not** verified end to end, since no Docker daemon was available while writing it (the wheel
build and layer contents are verified):

```bash
docker build -t cheiron .
docker run --rm -p 8000:8000 -e LLM_ENABLED=false cheiron
```

Then open **<http://localhost:8000>** for a demo UI that renders whatever the API returns, or
POST to `/analyze` directly. FastAPI's generated docs are at `/docs`.

It also runs with **no API key at all**. The deterministic planner covers eleven question
shapes — phase, status, trend, country, sponsor, sponsor class, study type, intervention type,
condition, enrollment size, and co-occurrence — so every example in §4 is reproducible in that
mode:

```bash
LLM_ENABLED=false uv run uvicorn app.main:create_app --factory
curl localhost:8000/health
# {"status":"ok","llm_enabled":false,"vocabulary":"ok","cache":{...}}
```

| Env var | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | — | Required only when `LLM_ENABLED=true` |
| `LLM_ENABLED` | `true` | `false` runs the deterministic planner alone, with no SDK import |
| `OPENAI_MODEL` | `gpt-4.1` | Planning model |
| `CTG_BASE_URL` | `https://clinicaltrials.gov/api/v2` | |
| `REQUEST_BUDGET_MS` | `10000` | Per-request wall clock |
| `MAX_UPSTREAM_REQUESTS` | `40` | Upstream requests per analysis |
| `MAX_CONCURRENCY` | `8` | Concurrent upstream requests |
| `RECORD_MODE_THRESHOLD` | `2000` | Below this, every record is read (§4) |
| `SAMPLE_PAGES` | `3` | Label-discovery pages in sampling mode |

---

## 2. Request schema

`POST /analyze` · `Content-Type: application/json`

**Unknown top-level fields are rejected with 422**, not ignored — a typo'd filter that silently
does nothing is worse than an error.

| Field | Type | Required | Validation | Maps to |
|---|---|---|---|---|
| `query` | string | **yes** | 3–1000 chars after trim | The question; planned, never pattern-matched into filters |
| `drug_name` | string | no | ≤200 chars | `query.intr` |
| `condition` | string | no | ≤200 chars | `query.cond` |
| `sponsor` | string | no | ≤200 chars | `query.lead` (lead sponsor only — see §5) |
| `country` | string | no | ≤100 chars | `AREA[LocationCountry]` |
| `phase` | string[] | no | each ∈ live `Phase` enum | `AREA[Phase]` |
| `status` | string[] | no | each ∈ live `Status` enum | `AREA[OverallStatus]` |
| `study_type` | string | no | ∈ live `StudyType` enum | `AREA[StudyType]` |
| `start_year` / `end_year` | int | no | 1900–2100; `start ≤ end` | `AREA[StartDate]RANGE[...]` |
| `options` | object | no | below | |

Enum values are validated against the **live** vocabulary loaded from `/studies/enums`, never a
hardcoded list. Structured fields are **hard constraints**: they override anything the planner
infers, and a model that contradicts them is overruled and the override recorded in
`meta.assumptions`.

### `options`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `max_buckets` | int 1–100 | `20` | Categories before an "Other" rollup |
| `include_citations` | bool | `true` | Attach per-datum source references |
| `citations_per_datum` | int 0–10 | `3` | Sampled NCT references per datum |
| `explain` | bool | `false` | Include the `AnalysisPlan` and upstream URL log in `meta` |

---

## 3. Response schema

```jsonc
{
  "visualization": {
    "type": "bar_chart",              // §6 lists all types
    "title": "…",                     // derived from the plan; never model prose
    "subtitle": "2,927 studies · ClinicalTrials.gov, data as of 2026-08-14",
    "encoding": { "x": {…}, "y": {…} },
    "data":  [ … ],                   // flat rows; {nodes, edges} for network_graph
    "annotations": [ … ]              // rollups, pruning, overlap notes
  },
  "meta": { … }
}
```

**Encoding channel** = `{field, type, label, sort?, format?, scale?}` where `type` ∈
`nominal | ordinal | quantitative | temporal | geo`. `field` always names a key present in
**every** `data` row — a response model validator enforces it, so a renderer needs nothing
beyond `encoding` + `data`.

| Chart type | Channels |
|---|---|
| `bar_chart` | `x`, `y` |
| `grouped_bar_chart` | `x`, `y`, `series` |
| `stacked_bar_chart` | `x`, `y`, `stack` |
| `time_series` | `x` (temporal), `y`, `series?` |
| `histogram` | `x` (ordinal bin labels, with `bin_start`/`bin_end` on each row), `y` |
| `scatter_plot` | `x`, `y`, `color?` |
| `choropleth_map` | `location` (ISO-3166 alpha-3 + raw name), `value` |
| `network_graph` | `nodes[{id,label,group,weight}]`, `edges[{source,target,weight}]` |
| `table` | `columns[{field,label,type}]` |
| `kpi` | `value`, `label`, `unit?` |

### `meta`

| Field | Meaning |
|---|---|
| `interpretation` | What was computed, in words — never a result |
| `planner` | `llm` \| `llm_repaired` \| `heuristic_fallback` |
| `filters_applied` | The filters that actually ran |
| `assumptions` | Choices a reader would otherwise have to guess at (§5) |
| `warnings` | Every degradation, rollup, sample, and downgrade, with numbers |
| `total_matching_studies` | Exact count for the base filter |
| `coverage` | Below — present on every response |
| `provenance` | `source`, `api_version`, `data_timestamp`, `retrieved_at` |
| `timing_ms` | `plan`, `retrieve`, `total` |
| `api_query_log`, `plan` | Only when `options.explain` |

`coverage` carries all seven fields on every response: `aggregation_mode`,
`groupby_semantics` (`partition` \| `overlapping`), `bucket_sum`, `unclassified_count`,
`overlap_note`, `sample_size`, `sample_coverage`.

### Errors

`text/plain` upstream errors are translated into a structured envelope:

```jsonc
{ "error": { "code": "upstream_timeout", "message": "…", "request_id": "…",
             "retry_after_seconds": 5, "details": [ … ] } }
```

| Status | `code` | When |
|---|---|---|
| 422 | `invalid_request` | Schema/field validation; `details[]` names each field |
| 422 | `unplannable_query` | Well-formed request, unanswerable question; suggestions included |
| 429 | `rate_limited` | Upstream asked us to slow down; `retry_after_seconds` from its header |
| 502 | `upstream_error` | ClinicalTrials.gov returned an error |
| 503 | `upstream_circuit_open` | Breaker open |
| 504 | `upstream_timeout` | Budget exceeded |
| 500 | `internal_error` | Our bug. Never attributed to upstream |

---

## 4. Example runs

Real output from live runs on 2026-08-16, trimmed for length. All of them run with
`LLM_ENABLED=false` — the deterministic planner reaches every one — except the scatter, whose
intent only the model emits.

### 4.1 Distribution — and numbers that legitimately do not add up

```bash
curl -s localhost:8000/analyze -H 'content-type: application/json' \
  -d '{"query":"How many trials by phase?","drug_name":"Pembrolizumab"}'
```

```jsonc
{
  "visualization": {
    "type": "bar_chart",
    "title": "Pembrolizumab Trials by Phase",
    "subtitle": "2,927 studies · ClinicalTrials.gov, data as of 2026-08-14",
    "encoding": {
      "x": { "field": "phase", "type": "ordinal", "label": "Trial phase",
             "sort": ["EARLY_PHASE1","PHASE1","PHASE2","PHASE3","PHASE4","NA","MISSING"] },
      "y": { "field": "study_count", "type": "quantitative", "label": "Number of trials" }
    },
    "data": [
      { "phase": "PHASE1", "phase_label": "Phase 1", "study_count": 1039, "exactness": "exact",
        "citations": [
          { "nct_id": "NCT05053880",
            "field": "protocolSection.designModule.phases",
            "excerpt": "[\"PHASE1\",\"PHASE2\"]",
            "url": "https://clinicaltrials.gov/study/NCT05053880" }
        ],
        "citation_note": "3 of 1,039 contributing studies" }
      // EARLY_PHASE1 51 · PHASE2 1750 · PHASE3 363 · PHASE4 17 · NA 53
    ]
  },
  "meta": {
    "coverage": {
      "aggregation_mode": "server_counts",
      "groupby_semantics": "overlapping",
      "bucket_sum": 3273,
      "unclassified_count": 169,
      "overlap_note": "phases is multi-valued: 2,758 studies carry ≥1 phase and contribute
                       3,273 bucket memberships (overlap 515). Bucket counts are each exact;
                       they do not sum to the total."
    }
  }
}
```

Buckets sum to **3,273** against a total of **2,927**. Not an error — and the response says so
in numbers rather than leaving you to notice. Note also what is **absent**: no `share_of_total`,
no percentage. A share implies a whole, and overlapping buckets have none; the response model
rejects those keys outright when `groupby_semantics` is `overlapping`.

### 4.2 Trend

```bash
curl -s localhost:8000/analyze -H 'content-type: application/json' \
  -d '{"query":"How has this changed over time?","drug_name":"Pembrolizumab",
       "start_year":2019,"end_year":2025}'
```

```jsonc
{ "type": "time_series", "title": "Pembrolizumab Trials by Start year",
  "subtitle": "1,845 studies · ClinicalTrials.gov, data as of 2026-08-14",
  "data": [
    { "start_year": "2019", "study_count": 247, "exactness": "exact" },
    { "start_year": "2020", "study_count": 263, "exactness": "exact" },
    { "start_year": "2021", "study_count": 265, "exactness": "exact" },
    { "start_year": "2022", "study_count": 298, "exactness": "exact" },
    { "start_year": "2023", "study_count": 259, "exactness": "exact" },
    { "start_year": "2024", "study_count": 256, "exactness": "exact" },
    { "start_year": "2025", "study_count": 257, "exactness": "exact" }
  ] }
```

### 4.3 A partition — where the buckets *do* reconcile

```bash
curl -s localhost:8000/analyze -H 'content-type: application/json' \
  -d '{"query":"What is the recruitment status of these trials?","drug_name":"Pembrolizumab"}'
```

```jsonc
{ "type": "bar_chart", "title": "Pembrolizumab Trials by Recruitment status",
  "subtitle": "2,927 studies · ClinicalTrials.gov, data as of 2026-08-14",
  "data": [
    { "overall_status": "ACTIVE_NOT_RECRUITING", "overall_status_label": "Active, not recruiting",
      "study_count": 480, "exactness": "exact" },
    { "overall_status": "COMPLETED",   "overall_status_label": "Completed",   "study_count": 829 },
    { "overall_status": "RECRUITING",  "overall_status_label": "Recruiting",  "study_count": 711 },
    { "overall_status": "NOT_YET_RECRUITING", "overall_status_label": "Not yet recruiting",
      "study_count": 152 }
    // …
  ] }
```

`overall_status` is single-valued, so `groupby_semantics` is `partition` here and the buckets
reconcile against the total. The same question shape gives a different coverage story than 4.1 —
that difference is the registry's `partition` flag, not a special case.

### 4.4 Geography — sampled, then confirmed exactly

```bash
curl -s localhost:8000/analyze -H 'content-type: application/json' \
  -d '{"query":"Which countries run these trials?","condition":"glioblastoma",
       "options":{"max_buckets":8}}'
```

```jsonc
{ "type": "choropleth_map", "title": "Glioblastoma Trials by Country",
  "subtitle": "2,251 studies · ClinicalTrials.gov, data as of 2026-08-14",
  "data": [
    { "country": "United States", "iso_a3": "USA", "study_count": 1373, "exactness": "exact" },
    { "country": "France",        "iso_a3": "FRA", "study_count": 155,  "exactness": "exact" },
    { "country": "China",         "iso_a3": "CHN", "study_count": 147,  "exactness": "exact" },
    { "country": "Canada",        "iso_a3": "CAN", "study_count": 137,  "exactness": "exact" },
    { "country": "Germany",       "iso_a3": "DEU", "study_count": 137,  "exactness": "exact" }
  ] }
```

`meta.coverage.aggregation_mode` is `sampled_then_confirmed`: country is an open vocabulary and
the result exceeds the record-mode threshold, so labels were **discovered** from a sample and
every count then **confirmed** exactly against the corpus. Here the sample happened to reach all
2,251 studies, and the warning says so rather than hedging:

> Labels were discovered from all 2,251 matching studies, so the label set is complete. Each
> displayed count is exact and confirmed against the full corpus.

### 4.5 Network graph — co-occurrence, with checkable citations

```bash
curl -s localhost:8000/analyze -H 'content-type: application/json' \
  -d '{"query":"Which interventions are studied together?","condition":"glioblastoma",
       "start_year":2024,"end_year":2025,"options":{"citations_per_datum":2}}'
```

```jsonc
{ "type": "network_graph", "title": "Glioblastoma Condition-Intervention Network",
  "subtitle": "229 studies · ClinicalTrials.gov, data as of 2026-08-14",
  "data": {
    "nodes": [
      { "id": "Glioblastoma",  "group": "condition",    "weight": 100 },
      { "id": "Temozolomide",  "group": "intervention", "weight": 20 },
      { "id": "Biospecimen Collection", "group": "intervention", "weight": 13 }
    ],
    "edges": [
      { "source": "Glioblastoma", "target": "Temozolomide", "weight": 11,
        "citations": [
          { "nct_id": "NCT07195591",
            "field": "protocolSection.conditionsModule.conditions + protocolSection.armsInterventionsModule.interventions[].name",
            "excerpt": "{\"conditions\":[\"Glioblastoma\"],\"interventions\":[\"GammaTile®\",\"External Beam Radiation Therapy\",\"Temozolomide\"]}",
            "url": "https://clinicaltrials.gov/study/NCT07195591" }
        ] }
    ]
  } }
```

Network graphs are **only** offered in `complete_records` mode, where co-occurrence is computed
from every matching study and is therefore exact and unbiased. Above the threshold the service
downgrades to a grouped bar chart and says why. A network built from a relevance-ranked sample
would look authoritative and would not be, so it is not offered at all.

### 4.6 Histogram — enrollment distribution

Plan: `intent=histogram`, `group_by=enrollment_count`, glioblastoma trials starting 2024–2025.

```jsonc
{ "type": "histogram", "title": "Glioblastoma Trials by Enrollment",
  "encoding": {
    "x": { "field": "enrollment_count", "type": "quantitative", "label": "Enrollment",
           "bin_start": "bin_start", "bin_end": "bin_end",
           "sort": ["0-10","11-50","51-100","101-500","501-1,000","1,001-5,000","5,001+"] },
    "y": { "field": "study_count", "type": "quantitative", "label": "Number of trials" }
  },
  "data": [
    { "enrollment_count": "0-10",      "study_count": 31,  "bin_start": 0,   "bin_end": 10 },
    { "enrollment_count": "11-50",     "study_count": 107, "bin_start": 11,  "bin_end": 50 },
    { "enrollment_count": "51-100",    "study_count": 38,  "bin_start": 51,  "bin_end": 100 },
    { "enrollment_count": "101-500",   "study_count": 45,  "bin_start": 101, "bin_end": 500 },
    { "enrollment_count": "501-1,000", "study_count": 7,   "bin_start": 501, "bin_end": 1000 }
  ] }
```

### 4.7 Scatter — one point per study

Plan: `intent=scatter`, same filter. Record mode only, so every point is a real study you can open.

```jsonc
{ "type": "scatter_plot", "title": "Glioblastoma Trials: enrollment by start year",
  "encoding": {
    "x": { "field": "start_year", "type": "temporal", "label": "Start year" },
    "y": { "field": "enrollment", "type": "quantitative", "label": "Enrollment" },
    "color": { "field": "nct_id", "type": "nominal", "label": "Study" }
  },
  "data": [
    { "nct_id": "NCT02546102", "start_year": 2024, "enrollment": 234,
      "url": "https://clinicaltrials.gov/study/NCT02546102" },
    { "nct_id": "NCT04945148", "start_year": 2024, "enrollment": 640,
      "url": "https://clinicaltrials.gov/study/NCT04945148" }
  ],
  "annotations": [
    { "type": "points", "plotted": 229, "excluded": 0,
      "text": "229 of 229 studies plotted; 0 lack a start date or an enrollment count and are
               excluded rather than plotted at zero." }
  ] }
```

---

## 5. How it works

```
validate → plan → PREFLIGHT → { DISCOVER } → COUNT → CITE → aggregate → render
```

One `countTotal` request decides how the whole analysis runs:

| Matching studies | Mode | Behaviour |
|---|---|---|
| ≤ 2,000 | `complete_records` | Page every record with a projection, aggregate in process. Exact, unbiased, all dimensions at once, citations and cross-tabs free |
| > 2,000, closed vocabulary | `server_counts` | One count-only request per bucket, concurrently. Exact; cost independent of result size |
| > 2,000, open vocabulary | `sampled_then_confirmed` | Sample → top-K labels → confirm each with `COVERAGE[FullMatch]`. Counts exact; label set may be incomplete, and says so |

**The 2,000 threshold is a property of upstream paging, not a tuning knob.** `pageToken` chains
are strictly serial — a token is only valid for the exact parameter set that produced it, so
pages cannot be fetched in parallel. 2,000 studies is two round trips and about a second; 50,000
is fifty round trips and 25–50 seconds, which does not fit on a request path.

### Key design decisions and trade-offs

- **The model plans; it never counts.** The cost is that a question outside the plan schema
  cannot be answered at all. The benefit is that no number in any response can be hallucinated,
  and the schema is small enough to validate exhaustively against the live vocabulary.
- **A deterministic planner exists alongside the LLM**, covering five template question shapes.
  It is the reference implementation the LLM path is tested against, and it keeps the service
  fully functional with no API key. The trade-off is duplicated intent logic.
- **`aggFilters` is never used.** `aggFilters=phase:na` returns 0 silently with HTTP 200 while
  `AREA[Phase]NA` returns 234,433. Every predicate goes through one Essie builder, and a test
  fails the build if the string `aggFilters` appears anywhere in `app/`.
- **`sponsor` means lead sponsor** (`query.lead`), not lead + collaborators (`query.spons`).
  These differ materially — `Pfizer` returns 3,862 versus 6,064. The choice is disclosed in
  `meta.assumptions` on every response that uses it.
- **Partial aggregations are never rendered.** If one bucket query in a fan-out fails, the whole
  group-by fails. A chart with a silently missing bar is worse than an error. Citations invert
  this: a missing citation is missing evidence for a still-exact number, so it warns and
  continues.
- **Coherence is refused at the plan, not patched at the renderer.** A comparison of an
  enrollment metric, a plan carrying both `series` and `secondary_group_by`, a one-element
  `series` — each is rejected with a reason rather than partly honoured. Every one of those was
  a real defect found by review: they produced, respectively, study counts labelled
  "participants", a silently dropped breakdown, and filters that were never applied.
- **Refuse rather than approximate.** A cross-tab that does not fit the request budget returns
  `unplannable_query` with the arithmetic (`84 cells needed, 24 requests available`) instead of
  truncating into a stacked bar whose segments do not sum to their bar.

---

## 6. Visualization coverage

The demo at `/` is the shortest way to see all of this: it renders every chart type from
`encoding` + `data` alone, in vanilla JS with inline SVG and no dependencies. That is deliberate
— a test asserts its renderer map covers every `ChartType` and that no renderer branches on a
drug name, a condition, or a dimension key, so anything it cannot draw is a gap in the
specification rather than in the page.


All ten types in the contract: `bar_chart`, `grouped_bar_chart`, `stacked_bar_chart`,
`time_series`, `histogram`, `scatter_plot`, `choropleth_map`, `network_graph`, `table`, `kpi`.
Chart choice is a deterministic function of
`(intent, cardinality, series count, partition?, mode)`; `viz_hint` from the model breaks ties
only and is discarded when it violates a safety rule, with the discard reported.

Three safety rules are non-overridable: no pie or 100%-stacked chart on a multi-valued
dimension, no share-of-total field under overlapping semantics, and no network graph outside
`complete_records`.

`histogram` and `scatter_plot` are reachable through the `enrollment_count` dimension. The
histogram bins enrollment with fixed, trial-shaped edges (0-10 … 5,001+) rather than equal
widths — enrollment spans 0 to 188,814,085, so linear bins would put nearly every study in the
first one — and bars are ordered by their lower edge, never by height. The scatter plots one
point per study, enrollment against start date, and therefore needs `complete_records` mode
exactly as `network_graph` does; above the threshold it downgrades to the histogram of the same
dimension and says so. Studies missing either axis are excluded and counted in an annotation
rather than plotted at zero, which would manufacture a cluster that does not exist.

---

## 7. The interesting problems

The theme, and the thing worth taking away: **identical syntax, different scope, plausible wrong
number.** This API punishes that pattern repeatedly, and none of the three below errors — each
returns HTTP 200 with a number that looks fine.

1. **A missing `AREA[]` prefix.** `COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` is valid Essie
   and returns **4,591**; correctly scoped it returns **1,841**. Both of my own documents
   originally recorded the unscoped form. The builder now takes `area` as a required positional
   argument, so the wrong expression is unconstructible.
2. **The same expression under a different parameter.** `(head OR neck) AND pain NOT cancer`
   returns **2,075** under `query.cond` and **9,964** under `filter.advanced`. Free-text filters
   go to `query.*` and bucket predicates to `filter.advanced`; folding one into the other moves
   the total while leaving the buckets untouched, so reconciliation fails with no indication
   which half is wrong.
3. **Substring matching on free text.** `AREA[LeadSponsorName]"Merck"` returns **2,733**,
   matching Merck KGaA and others, where the exact count is **1,841**.

Three more that shaped the implementation:

4. **`aggFilters=phase:na` returns 0, silently.** A silent zero is the worst possible failure for
   an aggregation engine.
5. **Country names are upstream's, not ISO's.** A live geography query returned a table instead
   of a map because one name in twenty had no code — "South Korea", which ISO spells "Korea,
   Republic of". The map is now generated from the corpus's own 226 distinct values, keys
   included: a curly apostrophe in "Côte d'Ivoire", and a genuine trailing space in "Bonaire,
   Saint Eustatius and Saba " that would break the lookup if tidied away.
6. **`NA` is not missing.** 234,433 studies carry an explicit `"NA"` phase; 141,903 have no
   `phases` field at all. Two distinct buckets, never merged.
7. **The 16% nobody counts.** 95,740 studies have status `UNKNOWN` — they stopped updating.
   `LastKnownStatus` is populated for exactly that cohort and nobody else (`MISSING` = 502,950
   of 598,690; `UNKNOWN AND NOT MISSING` = 95,740, so the overlap is total). Their last
   self-reported state was `RECRUITING` 54,030, `NOT_YET_RECRUITING` 23,607,
   `ACTIVE_NOT_RECRUITING` 14,458 — and `COMPLETED` **zero**, since a completed trial has no
   reason to lapse. The trials that go dark are overwhelmingly the ones still running.

---

## 8. Tools, validation, and what was deliberate

**Tools.** Claude (Claude Code) throughout, and Cursor for parts of the implementation. The
working method was to write the contract first — `SPEC.md` and `docs/CTG-API-NOTES.md` — then
implement against it in twelve reviewable commits, one per subsystem. The commit log is the
build narrative and each message records the reasoning behind that step.

**How correctness was validated.**

- **Everything upstream was measured, not assumed.** `docs/CTG-API-NOTES.md` records live `curl`
  results with counts; anything unverified is marked `[unverified]`. When the notes and the code
  disagreed, the notes were re-measured first and corrected — which is how the missing `AREA[]`
  prefix and the `query.cond`/`filter.advanced` mix-up in §7 were found.
- **Acceptance tests pin the arithmetic**, not just the shape: A1 asserts the exact reconciliation
  (2,927 / 169 / 3,273 / overlap 515), A2 asserts 1,841 rather than 2,733.
- **Two independent implementations are cross-checked.** Record mode and the count fan-out
  aggregate the same closed dimension by completely different routes, and a test asserts they
  agree.
- **Mutation checks on the tests that matter.** The concurrency test was verified to fail when
  the cap is raised; the fail-all test to fail when a partial result is returned; the
  timestamp-recheck test to fail when it reads a cached value instead of a live one. A green test
  that cannot fail is worse than no test.
- **94% line coverage**, though the number is the weakest signal here: several of the defects
  below were found in fully covered code, by running it rather than by testing it.
- **A late review pass found nine defects** the spec-derived tests could not see, including one
  that returned a fabricated comparison chart. Each is now pinned by a regression test in
  `tests/unit/test_review_fixes.py`. They are listed there rather than quietly fixed, because
  the pattern in them is more useful than the fixes.

**Deliberate design versus generated code.** The architecture is deliberate and was decided
before implementation: the plan-only LLM boundary, the three-mode preflight and the reason the
threshold is 2,000, the dimension registry with `partition` as a first-class flag, the
fail-all-versus-fail-soft split, and the rule that every cap or sample is disclosed with actual
numbers. The upstream findings in §7 are original measurements. Implementation was largely
generated against those specs and then reviewed, corrected, and re-tested — the review pass in
particular changed real behaviour rather than style.

---

## 9. Limitations and what I would improve with more time

- **The live Structured Outputs call is unverified.** The API key available during the build
  returned 401 (authentication, not quota — exhausted quota returns 429 `insufficient_quota`), so
  the LLM planner's repair loop, fallback, and caching are all exercised through an injected
  completer. The published schema is asserted against the documented strict constraint set, so
  the first live call should be a confirmation rather than a discovery — but it has not been made.
- **The demo bundles no map topology**, so a choropleth renders there as a ranked list. The spec
  carries ISO-3166 alpha-3 codes precisely so a real renderer can join them to one.
- **Enrollment bin edges are fixed, not adaptive.** They suit oncology-scale trials; a corpus-wide
  question would be better served by quantile bins computed from the result set.
- **Caches are in-process.** Correct for a stateless service, but a shared store would make the
  plan cache useful across replicas.
- **Cross-tabs above the record-mode threshold need both dimensions closed**, and refuse
  otherwise. Sampling a secondary dimension would need per-cell confirmation to stay exact.
- **Condition grouping is literal.** MeSH hierarchy would let "cancer" roll up its subtypes
  instead of matching the string.
- **`[unverified]` items in the notes**: upstream rate limits (undocumented; the widely repeated
  "50 req/min" could not be confirmed from any official source) and `pageToken` expiry under long
  crawls. The client self-limits regardless — this is a public NIH service we do not own.

---

## 10. Tests

```bash
uv run pytest -q                              # unit + acceptance, no network
uv run pytest --cov=app --cov-report=term     # 94% line coverage
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

No test touches the network. Upstream is stubbed at the transport seam with recorded fixtures;
live calls live in `scripts/verify_upstream.py`, which is what the fixtures are recorded from.

`tests/acceptance/` pins SPEC §8's criteria as executable contract tests against the recorded
`dataTimestamp` — A1 reconciliation, A2 exact-match discipline, A3 sample disclosure, A4 no
fabrication, A5 planner determinism, A6 degraded mode, A7 safety rules — plus invariants that
sweep every response: no bare `truncated` flag anywhere, `meta.coverage` fully populated, and no
share field under overlapping semantics.

```bash
uv run python scripts/showcase.py           # all ten chart types, live, in one command
uv run python scripts/verify_upstream.py    # reproduce every count in the notes
uv run python scripts/record_fixtures.py    # re-record after an upstream refresh
```

`showcase.py` exists because "supports ten chart types" is a claim a reviewer should be able to
check rather than take on trust. It runs ten real queries against the live API and prints what
came back; six go through the deterministic planner with no key, and the four needing intents
the keyword matcher does not emit have their plan supplied through the same seam the tests use —
labelled per row. It has already earned itself twice, catching two bugs no fixture reached.
