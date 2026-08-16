# ClinicalTrials.gov v2 API — Verified Engineering Notes

Everything here was confirmed with live `curl` calls against
`https://clinicaltrials.gov/api/v2` on **2026-08-16**
(`apiVersion 2.0.5`, `dataTimestamp 2026-08-14T09:00:05`, corpus **598,690** studies).

Anything not verified is marked **[unverified]**. This file is the ground truth the
implementation is written against; if a claim here turns out to be wrong, fix it here
first, then fix the code.

- Machine-readable spec: **`https://clinicaltrials.gov/api/oas/v2`** (OpenAPI 3.0.3, YAML, ~81 KB).
  Note: `…/api/oas/v2.yaml` and `…/api/v2/openapi.yaml` are **404**. The HTML docs pages
  are an Angular SPA that renders this same spec, and they return their own HTML shell
  (HTTP 200) for any unknown `/assets/*` path — so a naive fetch looks like it succeeded.
- Data refreshes **Mon–Fri, ~14:00 UTC**. Poll `/version` → `dataTimestamp` to detect it.

---

## 1. The aggregation constraint (drives the whole architecture)

`/stats/field/values` returns **corpus-wide distributions only**. It accepts exactly two
parameters, `types` and `fields`, and rejects everything else:

```
GET /stats/field/values?fields=Phase&query.cond=cancer
→ 400  Invalid prefix in parameter name: query.cond
```

**There is no server-side GROUP BY for a filtered query.** But `countTotal=true` returns
an *exact* total for any query (no capping observed at any magnitude; the unfiltered
total exactly equals `/stats/size` → `totalStudies`).

So a grouped aggregation is reconstructed one of two ways:

- **Count fan-out** — one count-only request per bucket, issued concurrently:
  `?query.intr=X&filter.advanced=AREA[Phase]PHASE2&countTotal=true&pageSize=1&fields=NCTId`
  (~200 bytes, ~1 RTT total regardless of result-set size).
- **Record mode** — page `/studies` with a `fields=` projection and aggregate in-process.
  **Pages are strictly serial** (each needs the previous `nextPageToken`), so cost is
  `ceil(total/1000)` sequential round trips. ~1 s for 2k studies; ~25–50 s for 50k.

---

## 2. Bucket predicates: use Essie `AREA[]`, never `aggFilters`

They disagree on edge values. Verified:

| Predicate | Count |
|---|---|
| `aggFilters=phase:NA` | 234,433 |
| `aggFilters=phase:na` (lowercase) | **0 — silently, no error** |
| `filter.advanced=AREA[Phase]NA` | 234,433 |

Unknown `aggFilters` *facet* ids 400, but unknown *option keys* return an empty result
set with HTTP 200. Silent-zero is the worst possible failure mode for an aggregation
engine, so: **all bucket predicates go through the Essie builder.** `aggFilters` is not
used anywhere in the codebase.

### Verified Essie constructs

| Construct | Example | Count |
|---|---|---|
| Boolean + grouping | `(head OR neck) AND pain NOT cancer` | 2,075 |
| Phrase | `"breast cancer"` | 16,538 |
| Field match | `AREA[Phase]PHASE3` | 49,659 |
| Date range | `AREA[StartDate]RANGE[2020-01-01,2020-12-31]` | 33,574 |
| Numeric range | `AREA[EnrollmentCount]RANGE[500,MAX]` | 66,859 |
| Has-value | `AREA[ResultsFirstPostDate]RANGE[MIN,MAX]` | 79,695 |
| **Absent field** | `AREA[Phase]MISSING` | 141,903 |
| Partial date | `AREA[StartDate]2022` | 37,619 |
| **Exact field match** | `AREA[LeadSponsorName]COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` | 1,841 |
| List size | `AREA[Phase:size]2` | 24,549 |
| Location scoping | `SEARCH[Location](AREA[LocationCity]Boston AND AREA[LocationStatus]RECRUITING)` | 2,597 |
| Distance | `AREA[LocationGeoPoint]DISTANCE[42.36,-71.06,50mi]` | 29,070 |
| Whole corpus | `ALL` | 598,690 |

Precedence: terms → `NOT`/context ops → `AND` → `OR`. Parentheses override. Escape a
literal operator with a backslash (`\MISSING`).

⚠️ `DISTANCE(...)` with **parentheses** leaks a raw Java parser exception. Use
`DISTANCE[...]` (square brackets) inside Essie; `filter.geo` uses `distance(...)` with
parens. Two different syntaxes for the same concept — easy to get wrong.

⚠️ Official caveat: *"COVERAGE and EXPANSION operators are not fully implemented on the
modernized ClinicalTrials.gov."* They do execute, but treat as approximate.

### `COVERAGE[FullMatch]` is mandatory for open-vocabulary confirmation

`COVER` and `COVERAGE` are **exact aliases in the Essie grammar**, verified below. The
codebase uses `COVERAGE` everywhere — it matches the official caveat wording above and
SPEC §5.3 — pinned behind the single `FULL_MATCH_OP` constant in `app/constants.py` so the
spelling is never re-litigated at a call site.

Verified 2026-08-16, `apiVersion 2.0.5`, `dataTimestamp 2026-08-14T09:00:05`. All calls are
`GET /studies` with `countTotal=true&pageSize=1&fields=NCTId` and the expression
url-encoded into `filter.advanced`. Raw bodies:
`tests/fixtures/upstream/fullmatch_*.json` (index: `fullmatch_manifest.json`).

| `filter.advanced` | Status | Count | |
|---|---|---|---|
| `AREA[LeadSponsorName]COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` | 200 | **1841** | ← correct; what A2 asserts |
| `AREA[LeadSponsorName]COVER[FullMatch]"Merck Sharp & Dohme LLC"` | 200 | 1841 | alias — byte-identical result |
| `AREA[LeadSponsorName]"Merck"` | 200 | 2733 | substring; matches Merck KGaA etc. |
| `AREA[LeadSponsorName]"Merck Sharp & Dohme LLC"` | 200 | 2170 | full name, no operator — **still wrong** |
| `COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"` | 200 | 4591 | **no `AREA[]` prefix → default search areas** |
| `AREA[LeadSponsorName]COVERAGE[FullMatch]Merck` | 200 | 0 | honest 0: no sponsor is named exactly `Merck` |
| `AREA[LeadSponsorName]COV[FullMatch]…` | 400 | — | `mismatched input '['` — not prefix-matched |
| `AREA[LeadSponsorName]COVERAGEX[FullMatch]…` | 400 | — | `mismatched input '['` |
| `AREA[LeadSponsorName]COVERAGE[FullMatc]…` | 400 | — | `Invalid coverage: FullMatc` |

Three consequences for the implementation:

1. **The `AREA[<field>]` prefix is not optional.** A bare `COVERAGE[FullMatch]"…"` is
   valid Essie and returns 4591 with HTTP 200 — a 2.5× overcount that looks like a
   success. The builder must never emit `COVERAGE` without an enclosing `AREA[]`.
2. **The operator itself fails loudly**, unlike `aggFilters`: a misspelled operator or
   coverage argument is a 400 with `text/plain`, never a silent zero.
3. **Confirmation must pass the discovered label verbatim.** `FullMatch` against a partial
   token returns a legitimate `0` at HTTP 200, so a truncated or normalized label silently
   drops the bucket to zero rather than erroring.

Confirming a discovered label without `FullMatch` inflates every ambiguous name.

---

## 3. `/studies` parameters that matter

| Parameter | Notes |
|---|---|
| `query.cond` `query.intr` `query.term` `query.locn` `query.titles` `query.outc` `query.id` `query.patient` | Essie, per search area. Affect relevance ranking. |
| `query.spons` vs **`query.lead`** | `spons` = lead **+** collaborators; `lead` = lead only. `Pfizer` → **6,064 vs 3,862**. Sponsor questions are ambiguous — pick one and say which in `meta`. |
| `filter.advanced` / `postFilter.advanced` | Any Essie expression. Identical effect; `filter.*` vs `postFilter.*` differ only in ranking participation. |
| `filter.overallStatus` | Comma- or pipe-separated `Status` enums. |
| `filter.geo` | Only `distance(lat,lon,N[km|mi])`. Radius 1 mi – 500 mi. |
| `fields` | Comma/pipe list of piece names (`NCTId|BriefTitle`), dotted paths, branch nodes, or `@query`. Unmatched leaves yield empty parents, not errors. Full study averages **17.3 KB**; a projected study is **~550 B**. |
| `sort` | **Max 2 items**; date/numeric fields + `@relevance` only. Text fields → 400. **No default sort** ("for a performance reason"). |
| `countTotal` | Exact. First page only — ignored on subsequent pages. CSV: `x-total-count` header. |
| `pageSize` | Default 10. **Silently clamped to 1000** — `pageSize=5000` returns 1000 with HTTP 200 and no warning. |
| `pageToken` | Opaque. Subsequent pages **must repeat every parameter** except `countTotal`/`pageSize`/`pageToken`. |

⚠️ **`pageToken` reuse across different queries does not error** — it silently returns
results for the *new* query at the old offset. The client must bind each token to a hash
of the parameter set that produced it and refuse a mismatch.

---

## 4. Transport

- **Errors are `text/plain`, not JSON.** e.g. `` `bogusParam` is unknown parameter ``,
  `Unsupported sort field type: text`, `NCT number NCT99999999 not found`,
  `Error parsing query in advanced filter: Unknown area name: 'X'`. Parse on status code
  + body string, never `response.json()`.
- **ETag** present and `If-None-Match` → **304** works. The tag looks dataset-scoped
  rather than per-query.
- **gzip** honored via `Accept-Encoding`. HTTP/2.
- No `Cache-Control`/`Last-Modified`. Use `/version` `dataTimestamp` for invalidation.
- **Rate limits: undocumented.** No `x-ratelimit-*` or `Retry-After` headers seen; 40
  back-to-back requests all returned 200. The widely-repeated "50 req/min" figure could
  not be confirmed from any official source — **treat as hearsay** and self-limit anyway.
  This is a public NIH service we do not own. **[unverified]**
- `pageToken` expiry horizon and behaviour under multi-hour crawls **[unverified]**.

---

## 5. Field paths for analytics

| Concept | Path | Cardinality |
|---|---|---|
| NCT id | `protocolSection.identificationModule.nctId` | 1 |
| Brief title | `…identificationModule.briefTitle` | 1 |
| Overall status | `protocolSection.statusModule.overallStatus` | 1 |
| Study type | `protocolSection.designModule.studyType` | 1 |
| **Phases** | `protocolSection.designModule.phases` | **list** |
| Enrollment | `…designModule.enrollmentInfo.count` / `.type` | 1 |
| Start date | `…statusModule.startDateStruct.date` / `.type` | 1 |
| Lead sponsor | `…sponsorCollaboratorsModule.leadSponsor.name` / `.class` | 1 |
| Collaborators | `…sponsorCollaboratorsModule.collaborators[].name/.class` | **list** |
| **Conditions** | `protocolSection.conditionsModule.conditions` | **list** |
| **Interventions** | `…armsInterventionsModule.interventions[].type/.name` | **list** |
| **Locations** | `…contactsLocationsModule.locations[].facility/.city/.state/.country/.geoPoint` | **list** |
| Results posted | top-level `hasResults` (sibling of `protocolSection`) | 1 |
| Last update | `…statusModule.lastUpdatePostDateStruct.date` | 1 |

**Single-valued (safe to treat as a partition):** `overallStatus`, `studyType`,
`leadSponsor.name`, `leadSponsor.class`, `startDate`, `enrollmentInfo.count`.
**Multi-valued (buckets overlap):** `phases`, `conditions`, `interventions`,
`locations.country`, `collaborators`.

---

## 6. Data-quality gotchas (all observed in live responses)

1. **`NA` phase ≠ missing phase.** 234,433 studies have explicit `"NA"`; 141,903 have no
   `phases` field at all. Two distinct buckets. Conflating them is a silent bug.
2. **Phases is a list.** 24,549 studies have two. For `query.intr=pembrolizumab`:
   total **2927**, phase buckets sum **3220**, +explicit `NA` 53 = **3273**, and **169**
   have no phase field. Bucket sums legitimately exceed the total by ~12%.
3. **Date precision is mixed** — `yyyy-MM` *and* `yyyy-MM-dd`. 5,359 studies have no
   start date. Corpus min `1900-01`, max `2099-01-01` (i.e. garbage at both ends).
4. **Enrollment** missing on 7,133; max is **188,814,085** (NCT05697705) and there are
   `99999999` placeholders. Never plot raw enrollment without clamping/winsorizing.
5. **Sponsor names are free text** — 51,610 distinct lead sponsors; "Novartis" vs
   "Novartis Pharmaceuticals". Only `class` (INDUSTRY/NIH/OTHER/…) is reliably categorical.
6. **`UNKNOWN` status is 95,740 studies (16%)** — trials that stopped updating. Not
   active, not completed. Must be its own bucket.
7. `dateStruct.type` (ACTUAL vs ESTIMATED) is frequently absent; enrollment `type` absent
   on 17,131.
8. Very old records (e.g. NCT00000102) have empty `statusModule`/`designModule`.
9. `markupFormat=legacy` bodies contain CRLF; default `markdown` differs from the
   pre-2025 classic pipeline. Geopoints now come from a different geo database.

---

## 7. Enum values used by the planner

Loaded at runtime from `/studies/enums` (41 enums) — never hardcoded. Listed here for
reference only.

- **Phase** — `NA`, `EARLY_PHASE1`, `PHASE1`, `PHASE2`, `PHASE3`, `PHASE4`
- **Status** — `ACTIVE_NOT_RECRUITING`, `COMPLETED`, `ENROLLING_BY_INVITATION`,
  `NOT_YET_RECRUITING`, `RECRUITING`, `SUSPENDED`, `TERMINATED`, `WITHDRAWN`,
  `AVAILABLE`, `NO_LONGER_AVAILABLE`, `TEMPORARILY_NOT_AVAILABLE`,
  `APPROVED_FOR_MARKETING`, `WITHHELD`, `UNKNOWN`
- **StudyType** — `INTERVENTIONAL`, `OBSERVATIONAL`, `EXPANDED_ACCESS`
- **AgencyClass** — `NIH`, `FED`, `OTHER_GOV`, `INDIV`, `INDUSTRY`, `NETWORK`, `AMBIG`,
  `OTHER`, `UNKNOWN`
- **InterventionType** — 11 values incl. `DRUG`, `BIOLOGICAL`, `DEVICE`, `PROCEDURE`,
  `BEHAVIORAL`, `DIETARY_SUPPLEMENT`, `RADIATION`, `GENETIC`, `DIAGNOSTIC_TEST`, `OTHER`

Companion endpoints: `/studies/search-areas` (19 areas → which `query.*` param hits which
fields) and `/studies/metadata` (454-path field tree).
