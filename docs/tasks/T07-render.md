# T07 — Chart registry, encoding, and `/analyze` wired end-to-end

**Est. 30 min · depends on: T06 · unblocks: T08, T09, T10**

After this task the service actually works — with the heuristic planner, no LLM, real
numbers, real coverage. SPEC §6, §4.1, §4.4.

## `app/render/registry.py`

`select_chart(plan, bucketset, dim, options) -> tuple[ChartType, list[str]]` returning the
type plus any warnings it generated. A **deterministic function** of
`(intent, cardinality, series count, dim.partition, mode)` — SPEC §6.1's table in order:

| Condition | Type |
|---|---|
| `intent=trend` + temporal dim | `time_series` |
| `intent=distribution`, 1 series, cardinality ≤ `max_buckets` | `bar_chart` |
| `intent=comparison`, 2–4 series | `grouped_bar_chart` |
| `secondary_group_by` + partition | `stacked_bar_chart` |
| `secondary_group_by` + not partition | `grouped_bar_chart` |
| `intent=geo` | `choropleth_map` |
| `intent=network` + `complete_records` | `network_graph` |
| `intent=scatter` + two quantitative metrics | `scatter_plot` |
| `intent=histogram` + binned quantitative | `histogram` |
| single scalar result | `kpi` |
| `intent=list` or cardinality > `max_buckets` | `table` |

`viz_hint` breaks **ties only** and is discarded when it violates a safety rule — record
the discard in `warnings` so the caller learns why they didn't get what they asked for.

**`histogram` and `scatter_plot` are unreachable by construction, and that is asserted, not
assumed.** Both rows require a quantitative dimension; SPEC §5.1's registry defines none, so
T05's validation rejects those intents before the registry is ever consulted. Keep both rows
in the table — they are the contract, and they become live the moment a quantitative
dimension is added — but add a test that iterates every `(intent, cardinality, series,
partition, mode)` combination the registry can actually receive and asserts neither type is
ever returned. If someone later adds an `enrollment_count` dimension, that test fails and
tells them the two branches now need real coverage.

**Safety rules, non-overridable (SPEC §6.1):**
1. No pie / donut / 100%-stacked on a `partition=False` dimension.
2. No `share_of_total` field emitted when `groupby_semantics == "overlapping"`.
3. No `network_graph` outside `complete_records` — downgrade to `grouped_bar_chart` and
   warn with the reason and the observed total.

Each rule gets a test that asserts both the rejection *and* the warning text.

## `app/render/encode.py`

`render(plan, bucketset, chart_type, dim, ctx) -> Visualization`, one branch per type with
the exact channels from SPEC §6.2. Requirements:

- Every `encoding` channel's `field` exists in every `data` row (T04 validates this; make
  it true here).
- Rows carry both raw key and human label: `{"phase": "PHASE2", "phase_label": "Phase 2",
  "study_count": 1750, "exactness": "exact"}` per SPEC §4.
- `sort` on nominal/ordinal channels comes from `vocab.sort_order(dim.enum_name)`, so a
  phase axis is clinically ordered, not alphabetical (SPEC §4's `sort` array).
- Open-vocabulary axes sort by value descending, then key ascending for stability.
- `"Other" rollup`: when cardinality > `max_buckets`, keep the top `max_buckets - 1` by
  value and roll the rest into `{"key": "OTHER", "label": "Other (N categories)"}` with an
  annotation naming N and the summed value. Never drop a bucket silently.
- `title` derived from the plan by a format string (`"{intervention} Trials by {dim.label}"`,
  title-cased) — **never model-authored prose** (SPEC §4.1).
- `subtitle` = `"{total:,} studies · ClinicalTrials.gov, data as of {date}"`. It carries the
  provenance; always emit it.
- `choropleth_map` `location` channel carries both ISO-3166 alpha-3 and the raw upstream
  country name (`{"country": "France", "iso_a3": "FRA"}`); unmappable names go to a `table`
  fallback with a warning listing them.
- `kpi` when the plan resolves to a single scalar; `table` for `intent=list` — SPEC §4.4:
  the service **always** returns a valid spec, never prose.
- Zero results → empty `data`, `total_matching_studies: 0`, and a warning. Never a
  fabricated row (SPEC A4).

## `app/main.py` — `POST /analyze`

```
validate → plan → preflight → aggregate → coverage → select_chart → render → meta
```

- Per-request `CTGClient` scope so `query_log` is request-local.
- `meta` assembled per SPEC §4.3 in full: `interpretation` (from the plan),
  `planner`, `filters_applied`, `assumptions` (including the `query.lead` sponsor note from
  SPEC §2.1 whenever `sponsor` is used), `warnings`, `total_matching_studies`, `coverage`,
  `provenance` (`source`, `api_version`, `data_timestamp`, `retrieved_at`), `timing_ms`
  (`plan`, `retrieve`, `total`, measured with `perf_counter`).
- `api_query_log` **only** when `options.explain`; when `explain`, also include the
  `AnalysisPlan` (SPEC §2.2).
- `CheironError` → SPEC §4.5's envelope via the T01 handlers. `NotImplementedError` from an
  unlanded mode → `422 unplannable_query` with an honest explanation.
- **Planning failure is `unplannable_query`, never `invalid_request`** (both are 422, so the
  distinction is only visible in `code`). The request was well-formed; it's the question that
  couldn't be served. `invalid_request` is reserved for schema and field validation — a caller
  filtering on `code` to decide whether to fix their payload or rephrase their question needs
  these separated. T05 records this in `validate_plan`'s docstring; honour it at the route.

## Tests

- End-to-end over an injected `CTGTransport` (T02's seam) with recorded fixtures,
  `LLM_ENABLED=false`: `{"query": "How many trials by
  phase?", "drug_name": "Pembrolizumab"}` → a `bar_chart` matching SPEC §4's example
  structure, with A1's numbers.
- Chart selection table: one case per row, plus one per safety rule.
- Zero-result query → empty `data` + warning + `total_matching_studies: 0` (A4).
- `viz_hint="pie_chart"` on `phase` → discarded, `bar_chart` returned, warning present (A7).
- Cardinality 50 with `max_buckets=20` → 19 buckets + `Other`, annotation names the rolled
  count and sum.
- `options.explain=true` → `api_query_log` non-empty and every URL is a real CTG URL;
  `explain=false` → key absent entirely.
- Response validates against the T04 models (so the contract can't drift).

## Done when

A live `curl` against a running server produces SPEC §4's example response shape with
today's real numbers. Paste that output into the README in T12.
