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

## Run it

```bash
uv sync --extra dev                      # or: pip install -e '.[dev]'
cp .env.example .env                     # add OPENAI_API_KEY for natural-language questions
uv run uvicorn app.main:create_app --factory --reload
```

It also runs with **no API key at all**, serving the questions the deterministic planner covers:

```bash
LLM_ENABLED=false uv run uvicorn app.main:create_app --factory
curl localhost:8000/health
# {"status":"ok","llm_enabled":false,"vocabulary":"ok","cache":{...}}
```

---

## Three worked examples

Real output from a live run on 2026-08-16, trimmed for length.

### 1. A distribution, with the numbers that do not add up — and why

```bash
curl -s localhost:8000/analyze -H 'content-type: application/json' -d '{
  "query": "How many trials by phase?", "drug_name": "Pembrolizumab"
}'
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
      // … EARLY_PHASE1 51 · PHASE2 1750 · PHASE3 363 · PHASE4 17 · NA 53
    ]
  }
}
```

The buckets sum to **3,273** against a total of **2,927**. That is not an error, and the
response says so in numbers rather than leaving you to notice:

```jsonc
"coverage": {
  "aggregation_mode": "server_counts",
  "groupby_semantics": "overlapping",
  "bucket_sum": 3273,
  "unclassified_count": 169,
  "overlap_note": "phases is multi-valued: 2,758 studies carry ≥1 phase and contribute 3,273
                   bucket memberships (overlap 515). Bucket counts are each exact; they do not
                   sum to the total."
}
```

Note also what is **absent**: no `share_of_total`, no percentage. A share implies a whole, and
when buckets overlap there is no whole for them to be shares of. The response model rejects
those keys outright when `groupby_semantics` is `overlapping`, so the rule cannot be forgotten
in a renderer.

### 2. A trend

```bash
curl -s localhost:8000/analyze -H 'content-type: application/json' -d '{
  "query": "How has this changed over time?", "drug_name": "Pembrolizumab",
  "start_year": 2019, "end_year": 2025
}'
```

```jsonc
{ "type": "time_series",
  "title": "Pembrolizumab Trials by Start year",
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

### 3. Provenance on every response

```jsonc
"provenance": {
  "source": "clinicaltrials.gov",
  "api_version": "2.0.5",
  "data_timestamp": "2026-08-14T09:00:05",
  "retrieved_at": "2026-08-17T00:21:33Z"
},
"timing_ms": { "plan": 0, "retrieve": 420, "total": 420 }
```

`data_timestamp` is the dataset revision, not the request time. It is captured before a fan-out
and re-read live afterwards; if the daily refresh lands mid-flight the whole group-by is retried,
so no chart ever mixes two dataset versions.

---

## How it works

```
validate → plan → PREFLIGHT → { DISCOVER } → COUNT → CITE → aggregate → render
```

One `countTotal` request decides how the whole analysis runs:

| Matching studies | Mode | Behaviour |
|---|---|---|
| ≤ 2,000 | `complete_records` | Page every record with a projection, aggregate in process. Exact, unbiased, all dimensions at once, citations free |
| > 2,000, closed vocabulary | `server_counts` | One count-only request per bucket, concurrently. Exact; cost independent of result size |
| > 2,000, open vocabulary | `sampled_then_confirmed` | Sample → top-K labels → confirm each with `COVERAGE[FullMatch]`. Counts exact; label set may be incomplete, and says so |

**The 2,000 threshold is a property of upstream paging, not a tuning knob.** `pageToken` chains
are strictly serial — a token is only valid for the exact parameter set that produced it, so
pages cannot be fetched in parallel. 2,000 studies is two round trips and about a second; 50,000
is fifty round trips and 25–50 seconds, which does not fit on a request path.

---

## The interesting problems

The theme, and the thing worth taking away: **identical syntax, different scope, plausible wrong
number.** This API punishes that pattern repeatedly, and none of the three cases below errors —
each returns HTTP 200 with a number that looks fine.

1. **A missing `AREA[]` prefix.** `COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` is valid Essie
   and returns **4,591**; the correctly scoped `AREA[LeadSponsorName]COVERAGE[FullMatch]"…"`
   returns **1,841**. Both documents originally recorded the unscoped form. The builder now takes
   `area` as a required positional argument, so the wrong expression is unconstructible.
2. **The same expression under a different parameter.** `(head OR neck) AND pain NOT cancer`
   returns **2,075** under `query.cond` and **9,964** under `filter.advanced`; `"breast cancer"`
   returns 16,538 versus 17,819. Free-text filters go to `query.*` and bucket predicates go to
   `filter.advanced`, and folding one into the other moves the total while leaving the buckets
   untouched — reconciliation then fails with no indication which half is wrong.
3. **Substring matching on free text.** `AREA[LeadSponsorName]"Merck"` returns **2,733**,
   matching Merck KGaA and others, where the exact count is **1,841**.

Three more that shaped the implementation:

4. **`aggFilters=phase:na` returns 0, silently, with HTTP 200** — while `AREA[Phase]NA` returns
   234,433. A silent zero is the worst possible failure for an aggregation engine, so `aggFilters`
   appears nowhere in the codebase and a test enforces that.
5. **`NA` is not missing.** 234,433 studies carry an explicit `"NA"` phase; 141,903 have no
   `phases` field at all. Two distinct buckets, never merged — and the unclassified count is
   probed separately and always reported.
6. **The 16% nobody counts.** 95,740 studies (16% of the corpus) have status `UNKNOWN` — they
   stopped updating. `LastKnownStatus` turns out to be populated for exactly that cohort and
   nobody else (`MISSING` = 502,950 of 598,690; `UNKNOWN AND NOT MISSING` = 95,740, so the
   overlap is total). Their last self-reported state was `RECRUITING` 54,030,
   `NOT_YET_RECRUITING` 23,607, `ACTIVE_NOT_RECRUITING` 14,458 — and `COMPLETED` **zero**, since
   a completed trial has no reason to lapse. The trials that go dark are overwhelmingly the ones
   that were still running.

---

## Tests

```bash
uv run pytest -q          # unit + acceptance, no network
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

No test touches the network. Upstream is stubbed at the transport seam with recorded fixtures;
live calls live in `scripts/verify_upstream.py` and are what the fixtures are recorded from.

`tests/acceptance/` pins SPEC §8's criteria as executable contract tests against the recorded
`dataTimestamp` — A1 reconciliation, A2 exact-match discipline, A3 sample disclosure, A4 no
fabrication, A5 planner determinism, A6 degraded mode, A7 safety rules — plus invariants that
sweep every response: no bare `truncated` flag anywhere, `meta.coverage` fully populated, and no
share field under overlapping semantics.

To re-record fixtures after an upstream data refresh:

```bash
uv run python scripts/record_fixtures.py
uv run python scripts/verify_upstream.py       # reproduces every count in the notes
```

---

## What I'd do with more time

- **An `enrollment_count` dimension.** It is the one change that unlocks SPEC §6.1's `histogram`
  and `scatter_plot` rows, which are specified but unreachable today because the dimension
  registry defines nothing quantitative. Plans with those intents are refused with a message
  naming the real blocker rather than falling through to a table that answers a different
  question. Enrollment needs record-mode paging and winsorizing to be plottable at all: missing
  on 7,133 studies, `99999999` placeholders, and a maximum of 188,814,085.
- **A shared cache.** Both caches are in-process, which is correct for a stateless service that
  scales horizontally only if the cache is not authoritative — but a shared store would make the
  plan cache useful across replicas.
- **Live verification of the Structured Outputs call.** The key available during the build
  returned 401 on every request, so the LLM planner's repair loop, fallback, and caching are all
  exercised through an injected completer. The schema is asserted against the documented strict
  constraint set, so the first live call should be a confirmation rather than a discovery — but
  it has not been confirmed.
- **`postFilter` for ranking-aware series**, and condition-hierarchy grouping via MeSH so
  "cancer" can roll up its subtypes.
- **The `[unverified]` items in the notes**: upstream rate limits (undocumented; the widely
  repeated "50 req/min" could not be confirmed from any official source) and `pageToken` expiry
  behaviour under long crawls. The client self-limits regardless, since this is a public NIH
  service we do not own.
