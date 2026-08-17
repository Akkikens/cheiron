# Cheiron. Service Specification

Version 1.0 · Contract for `POST /analyze`

A natural-language question about clinical trials in; a **renderable visualization
specification**, with every number traceable to ClinicalTrials.gov, out.

This document is the authoritative contract. It is written to be implementable without
guessing: a frontend engineer builds a renderer from §4, and a backend engineer builds
the service from §5–§7. Verified API behaviour it depends on is in
[`docs/CTG-API-NOTES.md`](docs/CTG-API-NOTES.md).

---

## 1. Design thesis

> **The language model never emits a number, a label, or a fact.
> It emits only a validated query plan. Everything downstream is deterministic.**

The model's entire job is translating a question into an `AnalysisPlan` (§3) whose every
field is a closed enum or a search string. It never sees study data, so it cannot invent
a count. Retrieval, aggregation, chart selection, and citation are all deterministic code
operating on exact API responses.

Consequences that fall out of this one rule:

| Property | Why it follows |
|---|---|
| Numbers are never hallucinated | The model is not in the data path |
| Results are reproducible | Same plan + same `dataTimestamp` ⇒ byte-identical output |
| The system is testable | Assert on the plan, and on counts, separately |
| It is cheap | The plan is cacheable; most requests skip the model |
| It degrades gracefully | A rule-based planner can emit the same IR with no model at all |

---

## 2. Request

`POST /analyze` · `Content-Type: application/json`

```jsonc
{
  "query": "How has the number of trials for this drug changed over time?",  // required
  "drug_name": "Pembrolizumab",          // optional structured hints (§2.1)
  "condition": "melanoma",
  "sponsor": "Merck Sharp & Dohme LLC",
  "country": "France",
  "phase": ["PHASE2", "PHASE3"],
  "status": ["RECRUITING"],
  "study_type": "INTERVENTIONAL",
  "start_year": 2015,
  "end_year": 2025,
  "options": {                            // optional, §2.2
    "max_buckets": 20,
    "include_citations": true,
    "citations_per_datum": 3,
    "explain": false
  }
}
```

### 2.1 Fields

| Field | Type | Req. | Validation |
|---|---|---|---|
| `query` | string | **yes** | 3–1000 chars after trim; non-empty |
| `drug_name` | string | no | ≤200 chars → `query.intr` |
| `condition` | string | no | ≤200 chars → `query.cond` |
| `sponsor` | string | no | ≤200 chars → `query.lead` (lead sponsor; see note) |
| `country` | string | no | ≤100 chars → `AREA[LocationCountry]` |
| `phase` | `Phase[]` | no | each ∈ live `/studies/enums` `Phase` |
| `status` | `Status[]` | no | each ∈ live `/studies/enums` `Status` |
| `study_type` | `StudyType` | no | ∈ live enum |
| `start_year` / `end_year` | int | no | 1900–2100; `start_year ≤ end_year` |
| `options` | object | no | §2.2 |

Structured fields are **hard constraints**. They are applied verbatim and **override**
anything the planner infers from `query`; the model is told about them but cannot
contradict them. This is what makes `{"query": "...for this drug", "drug_name": "..."}`
work: the pronoun is resolved by the caller, not guessed.

> **`sponsor` maps to `query.lead` (lead sponsor only), not `query.spons` (lead +
> collaborators).** These differ materially: `Pfizer` returns 3,862 vs 6,064. The choice
> is recorded in `meta.assumptions` on every response that uses it.

Unknown top-level fields are **rejected** (`422`), not ignored: a typo'd filter that
silently does nothing is worse than an error.

### 2.2 `options`

| Field | Type | Default | Meaning |
|---|---|---|---|
| `max_buckets` | int 1–100 | 20 | Max categories plotted. The rest are still counted; `meta.coverage` reports how many and their total |
| `include_citations` | bool | `true` | Attach per-datum source references |
| `citations_per_datum` | int 0–10 | 3 | Sampled NCT references per datum |
| `explain` | bool | `false` | Include the `AnalysisPlan` and upstream URL log in `meta` |

---

## 3. `AnalysisPlan`: the internal IR

The **only** thing the model produces. Enforced by JSON Schema (`strict: true`), then
re-validated against the live vocabulary loaded from `/studies/enums`.

```jsonc
{
  "intent": "trend",
  "filters": {
    "condition": null, "intervention": "Pembrolizumab", "sponsor": null,
    "term": null, "country": null,
    "phase": [], "status": [], "study_type": null,
    "start_year": 2015, "end_year": 2025
  },
  "series": [],
  "group_by": { "dimension": "start_year", "bin": { "size": 1 } },
  "secondary_group_by": null,
  "metric": "study_count",
  "viz_hint": "time_series",
  "interpretation": "Annual count of interventional trials studying pembrolizumab, 2015–2025."
}
```

| Field | Type | Notes |
|---|---|---|
| `intent` | enum | `distribution` `trend` `comparison` `geo` `network` `scatter` `histogram` `list` |
| `filters` | `StudyFilter` | Same shape as §2.1's structured fields |
| `series` | `SeriesSpec[]` | Length > 1 ⇒ comparison. Each = `{label, filters}` overlay |
| `group_by` | `GroupBy` | `{dimension, bin?}`: §5.1 |
| `secondary_group_by` | `GroupBy?` | Present ⇒ grouped/stacked bar |
| `metric` | enum | `study_count` `enrollment_sum` `enrollment_median` |
| `viz_hint` | `ChartType?` | **Advisory only.** The registry (§6) decides; a hint that violates a safety rule is discarded |
| `interpretation` | string | ≤300 chars, surfaced as `meta.interpretation` |

**Validation & repair.** Schema violation, unknown enum value, or an incoherent
combination (e.g. `intent: network` with `metric: enrollment_median`) → the error text is
fed back to the model for at most **2** repair attempts. Still failing ⇒ fall back to the
deterministic planner (§5.5) and set `meta.planner: "heuristic_fallback"`. The request
never fails because the model misbehaved.

---

## 4. Response

`200 OK`

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
      { "phase": "PHASE2", "phase_label": "Phase 2", "study_count": 1750,
        "exactness": "exact",
        "citations": [
          { "nct_id": "NCT05053880",
            "field": "protocolSection.designModule.phases",
            "excerpt": "[\"PHASE1\",\"PHASE2\"]",
            "url": "https://clinicaltrials.gov/study/NCT05053880" }
        ] }
    ]
  },
  "meta": { }   // §4.3
}
```

### 4.1 `visualization`

| Field | Type | Notes |
|---|---|---|
| `type` | `ChartType` | §6 |
| `title` | string | Human-readable, derived from the plan: never model-authored prose |
| `subtitle` | string? | Total + data date. Renderers should show it; it carries the provenance |
| `encoding` | object | Channel → field mapping. Channels vary by type (§6.2) |
| `data` | object[] | Flat rows for most types; `{nodes, edges}` for `network_graph` |
| `annotations` | object[]? | e.g. `{type: "note", text: "Buckets overlap; see meta.coverage"}` |

**Encoding channel** = `{field, type, label, sort?, format?, scale?, unit?, bin_start?, bin_end?}`
where `type` ∈
`nominal | ordinal | quantitative | temporal | geo`. `field` always names a key present in
every `data` row. A renderer needs nothing beyond `encoding` + `data`.

### 4.2 Citations (bonus requirement)

Every datum carries up to `citations_per_datum` references:

```jsonc
{ "nct_id": "NCT05053880",
  "field": "protocolSection.designModule.phases",
  "excerpt": "[\"PHASE1\",\"PHASE2\"]",
  "url": "https://clinicaltrials.gov/study/NCT05053880" }
```

Rules that make these trustworthy:

1. `excerpt` is an **exact substring / verbatim serialization** of the upstream response.
   Never model-generated, never paraphrased.
2. `field` is the **structured field that determines bucket membership**: not a free-text
   summary scan. This matters: `query.intr=keytruda` matches via synonym expansion, so the
   literal string "keytruda" may appear nowhere in the record. Citing
   `interventions[].name` is always defensible; citing a `briefSummary` grep is not.
3. Citations are a **sample**, and say so: `datum.citation_note` reads
   `"3 of 1750 contributing studies"`. They are evidence, not enumeration.

### 4.3 `meta`: including honest coverage

```jsonc
{
  "interpretation": "Distribution of pembrolizumab trials across phases.",
  "planner": "llm" ,                     // "llm" | "llm_repaired" | "heuristic_fallback"
  "filters_applied": { "intervention": "Pembrolizumab" },
  // On a comparison, one entry per series instead, each the base filters with that series'
  // overlay applied:
  //   "filters_applied": { "series": [
  //     { "label": "Merck",  "intervention": "Pembrolizumab", "sponsor": "Merck Sharp & Dohme LLC" },
  //     { "label": "Pfizer", "intervention": "Pembrolizumab", "sponsor": "Pfizer" } ] }
  "assumptions": [
    "Sponsor matched against lead sponsor only (query.lead), excluding collaborators."
  ],
  "warnings": [],
  "total_matching_studies": 2927,
  "coverage": {
    "aggregation_mode": "server_counts",       // §5.2
    "groupby_semantics": "overlapping",        // "partition" | "overlapping"
    "bucket_sum": 3273,
    "unclassified_count": 169,
    "overlap_note": "phases is multi-valued: 2,758 studies carry ≥1 phase and contribute 3,273 bucket memberships (overlap 515). Bucket counts are each exact; they do not sum to the total.",
    "sample_size": null,
    "sample_coverage": null
  },
  "provenance": {
    "source": "clinicaltrials.gov",
    "api_version": "2.0.5",
    "data_timestamp": "2026-08-14T09:00:05",
    "retrieved_at": "2026-08-16T10:22:41Z"
  },
  "timing_ms": { "plan": 480, "retrieve": 610, "total": 1131 },
  "api_query_log": [ "https://clinicaltrials.gov/api/v2/studies?..." ]   // only when options.explain
}
```

**Coverage is a hard requirement, not decoration.** Rules:

- `groupby_semantics` is `"overlapping"` whenever the dimension is multi-valued (§5.1).
  When overlapping, `overlap_note` **must state the actual numbers**, as above. A bare
  `truncated: true` is not acceptable anywhere in this API: always truncated *what*, *at
  what*, *why*, and *whether the shown numbers are still exact*.
- `unclassified_count` is always present, from `AREA[<field>]MISSING`. A gap is never
  left invisible.
- For sampled open-vocabulary results, `sample_size` / `sample_coverage` are populated and
  a warning states that per-label counts are exact but the label *set* may be incomplete.

### 4.4 "No visualization needed"

If the question resolves to a single number or a lookup, `type` is `kpi` or `table`. The
service always returns a valid spec: it never returns prose instead. `intent: list` with
zero results yields an empty `table` plus a warning, never a fabricated row.

### 4.5 Errors

`text/plain` upstream errors are translated to structured JSON:

| Status | `code` | When |
|---|---|---|
| 422 | `invalid_request` | Schema/validation failure; `details[]` per field |
| 422 | `unplannable_query` | Question isn't answerable from trial metadata; suggestions included |
| 502 | `upstream_error` | ClinicalTrials.gov returned an error |
| 504 | `upstream_timeout` | Budget exceeded |
| 503 | `upstream_circuit_open` | Breaker open; `retry_after_seconds` set |
| 429 | `rate_limited` | **Upstream** asked us to slow down; `retry_after_seconds` carries its header value verbatim. The local token bucket waits rather than rejecting, so it never produces this |
| 500 | `internal_error` | Unhandled server-side fault. Generic message; `request_id` is the only actionable content. Never attributed to upstream: our bugs are ours |

```jsonc
{ "error": { "code": "upstream_timeout", "message": "…", "request_id": "…", "retry_after_seconds": 5 } }
```

**Partial aggregations are never rendered.** If any bucket query in a fan-out fails, the
whole group-by fails loudly. A chart with a silently missing bar is worse than an error.

---

## 5. Execution

One pipeline for every intent:

```
validate → plan → PREFLIGHT → { DISCOVER } → COUNT → CITE → aggregate → render
```

### 5.1 Dimension registry

Each groupable dimension is one registry entry: adding a question type means adding a
row here, not a new code path.

| Dimension | Field | Vocabulary | Partition? |
|---|---|---|---|
| `phase` | `Phase` | closed (enum) | **no**: multi-valued |
| `overall_status` | `OverallStatus` | closed | yes |
| `study_type` | `StudyType` | closed | yes |
| `sponsor_class` | `LeadSponsorClass` | closed | yes |
| `intervention_type` | `InterventionType` | closed | **no** |
| `start_year` | `StartDate` | closed (derived range) | yes |
| `country` | `LocationCountry` | open | **no** |
| `lead_sponsor` | `LeadSponsorName` | open | yes |
| `intervention_name` | `InterventionName` | open | **no** |
| `condition` | `Condition` | open | **no** |
| `enrollment_count` | `EnrollmentCount` | closed (binned range) | yes |

`partition: false` ⇒ `groupby_semantics: "overlapping"`, and the chart registry (§6)
**refuses pie / 100%-stacked / share-of-total** for that dimension.

### 5.2 PREFLIGHT decides the mode

One `countTotal=true&pageSize=1&fields=NCTId` request (~150 bytes) on the base filter:

| Condition | `aggregation_mode` | Behaviour |
|---|---|---|
| `total ≤ 2000` | `complete_records` | Page the full result set with a `fields=` projection (≤2 serial pages, ~1 s) and aggregate in process. **Exact, unbiased, all dimensions at once, citations free.** |
| `total > 2000`, closed vocab | `server_counts` | One count-only request per bucket, concurrently. Exact; cost independent of result size. |
| `total > 2000`, open vocab | `sampled_then_confirmed` | Sample N pages → top-K labels → confirm each with `COVERAGE[FullMatch]`. Counts exact; label set may be incomplete (disclosed). |

The 2000 threshold exists because **pageToken chains are serial**: 2k studies ≈ 2 round
trips, but 50k ≈ 50 round trips ≈ 25–50 s, which is not viable on a request path.

Every mode additionally issues `AREA[<field>]MISSING` for `unclassified_count`.

### 5.3 Bucket predicates

All predicates are built by a single Essie builder. **`aggFilters` is never used**: it
disagrees with Essie on edge values and fails *silently* on a bad token
(`aggFilters=phase:na` → 0, while `AREA[Phase]NA` → 234,433).

| Purpose | Predicate |
|---|---|
| Closed enum bucket | `AREA[Phase]PHASE2` |
| Open label bucket | `AREA[LeadSponsorName]COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` |
| Year bucket | `AREA[StartDate]RANGE[2018-01-01,2018-12-31]` |
| Unclassified | `AREA[Phase]MISSING` |
| Country | `AREA[LocationCountry]COVERAGE[FullMatch]"France"` |

`COVERAGE[FullMatch]` is **mandatory** for open vocabularies. Without it,
`AREA[LeadSponsorName]"Merck"` returns 2,733 (substring-matching Merck KGaA and others)
where the correct exact count is 1,841.

The builder escapes `"` `\` and the Essie operator keywords in all user-supplied text.
User input is never concatenated into an expression unescaped.

### 5.4 Network graph

Only available in `complete_records` mode (`total ≤ 2000`), where co-occurrence is
computed from the full result set and is therefore exact and unbiased. Outside that
regime the service downgrades to a grouped bar chart and records the reason in
`meta.warnings`. Sampled co-occurrence is not offered: a network graph built from a
relevance-ranked sample looks authoritative and isn't.

Node/edge sets: `sponsor ↔ intervention`, `intervention ↔ intervention` (co-occurring in
the same trial), `condition ↔ intervention`. Edges carry `weight` (# co-occurring trials)
and citations.

### 5.5 Deterministic fallback planner

Rule-based, no model. Covers ~5 template intents (by-phase, by-status, by-year, by-country,
by-sponsor) via keyword matching over the question plus the structured fields. Exists so
that:

- the service is fully functional with `LLM_ENABLED=false` and **no API key at all**;
- a model outage degrades coverage, not availability;
- every LLM path has a deterministic reference implementation to test against.

It is deliberately small and fully tested. It is not an attempt to reimplement the model.

---

## 6. Chart registry

### 6.1 Selection

Deterministic function of `(intent, dimension cardinality, series count, partition?)`.
`viz_hint` breaks ties only, and is discarded if it violates a safety rule.

| Condition | Type |
|---|---|
| `intent=trend`, temporal dimension | `time_series` |
| `intent=distribution`, 1 series, ≤ `max_buckets` | `bar_chart` |
| `intent=comparison`, 2–4 series | `grouped_bar_chart` |
| `secondary_group_by` present, partition | `stacked_bar_chart` |
| `secondary_group_by` present, **not** partition | `grouped_bar_chart` (stacking would imply a false whole) |
| `intent=geo` | `choropleth_map`, or `table` when too many country names have no ISO-3166 code to place |
| `intent=network`, `complete_records` | `network_graph` |
| `intent=scatter`, two quantitative metrics | `scatter_plot` |
| `intent=histogram`, binned quantitative | `histogram` |
| single scalar result | `kpi` |
| `intent=list` or cardinality > `max_buckets` | `table` |

**Safety rules (non-overridable).** No pie/donut/100%-stacked on a non-partition
dimension. No `share_of_total` field emitted when `groupby_semantics=overlapping`. No
`network_graph` outside `complete_records`.

> **`scatter_plot` and `histogram` became reachable when `enrollment_count` was added to §5.1.**
> Both require a quantitative `group_by` dimension. The refusal was always keyed on the registry
> rather than hardcoded, so adding one row lifted it; with the set empty again, the plan is
> refused with `unplannable_query` naming the blocker rather than falling through to `table` and
> silently answering a different question.
>
> A `histogram` bins enrollment with fixed, trial-shaped edges (0-10 … 5,001+) rather than equal
> widths, because enrollment spans 0 to 188,814,085 and linear bins would put nearly every study
> in the first one. A `scatter_plot` plots one point per study and therefore needs
> `complete_records` mode, exactly as `network_graph` does; above the threshold it downgrades to
> the histogram of the same dimension and says so.

### 6.2 Encoding per type

| Type | Channels |
|---|---|
| `bar_chart` | `x` (nominal), `y` (quantitative) |
| `grouped_bar_chart` | `x`, `y`, `series` |
| `stacked_bar_chart` | `x`, `y`, `stack` |
| `time_series` | `x` (temporal), `y`, `series?` |
| `scatter_plot` | `x`, `y`, `size?`, `color?` |
| `histogram` | `x` (binned, with `bin_start`/`bin_end`), `y` |
| `choropleth_map` | `location` (geo, ISO-3166 + raw name), `value` |
| `network_graph` | `nodes[{id,label,group,weight}]`, `edges[{source,target,weight}]` |
| `table` | `columns[{field,label,type}]` |
| `kpi` | `value`, `label`, `unit?` |

---

## 7. Non-functional

- **Stateless.** All state in caches; horizontal scale is trivial.
- **Caching** keyed on `(normalized plan, dataTimestamp)`. Upstream refreshes weekdays
  ~14:00 UTC, so entries stay valid ~24 h. A plan cache keyed on normalized question text
  lets repeat questions skip the model entirely.
- **Upstream courtesy.** ClinicalTrials.gov is a public NIH service we do not own and
  whose rate limits are undocumented. Hard concurrency cap, token bucket, circuit breaker,
  exponential backoff with jitter, gzip, and `If-None-Match`/304 revalidation.
- **`dataTimestamp` consistency.** Recorded before and after a fan-out; if the daily
  refresh lands mid-flight the whole group-by is retried, so no chart ever mixes two
  dataset versions.
- **Budgets.** Per-request wall clock ≤ 10 s; upstream requests per analysis ≤ 40; model
  calls ≤ 3 (1 + 2 repairs).

---

## 8. Acceptance criteria

These are executable tests, pinned to a recorded `dataTimestamp`.

**A1: reconciliation.** `query.intr=pembrolizumab`, group by phase:

```
total  2927 · MISSING 169 · with ≥1 phase 2758
buckets: NA 53 · EARLY_PHASE1 51 · PHASE1 1039 · PHASE2 1750 · PHASE3 363 · PHASE4 17
Σ 3273 · overlap 515 · PHASE1∩PHASE2 472
```
Response must report `bucket_sum: 3273`, `unclassified_count: 169`,
`groupby_semantics: "overlapping"`, and must **not** contain a share/percentage field.

`PHASE1∩PHASE2` is the one number here that no response can carry, because there is no
phase-by-phase cross-tab to put it in. `scripts/verify_upstream.py --a1` counts it live so
it is not quoted on trust: 472 of the 515 overlapping memberships are that single pairing,
which is what a phase 1/2 registration looks like in the data.

**A2: exact-match discipline.** `AREA[LeadSponsorName]"Merck"` = 2733 but
`AREA[LeadSponsorName]COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` = 1841. A sponsor bar
chart must report 1841. The `AREA[]` prefix is part of the predicate: the same expression
without it returns 4591 at HTTP 200.

**A3: no silent truncation.** A query with `total > 2000` and an open dimension must set
`aggregation_mode: "sampled_then_confirmed"`, populate `sample_coverage`, and emit a
warning. `pageSize` must never be sent above 1000 (it clamps silently).

**A4: no fabrication.** A query matching zero studies returns an empty `data` array,
`total_matching_studies: 0`, and a warning: never an invented row.

**A5: planner determinism.** A fixed question yields a stable `AnalysisPlan`. Golden set
of ~20 questions asserts the IR, not prose.

**A6: degraded mode.** With `LLM_ENABLED=false` the golden questions covered by the
fallback planner still return valid, correct specs.

**A7: safety rules.** `intent=network` with `total > 2000` downgrades and warns; a
`viz_hint` of `pie_chart` on a non-partition dimension is discarded.
