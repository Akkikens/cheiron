# T11 — `sampled_then_confirmed` mode

**Est. 25 min · depends on: T06 · cuttable: second (fall back to `table` + warning)**

SPEC §5.2, §4.3, A3; notes §2. The honest handling of open vocabularies at scale: **counts
exact, label set possibly incomplete, and the incompleteness disclosed in numbers.**

## `app/engine/modes/sampled.py`

For `total > 2000` on an open-vocabulary dimension (`country`, `lead_sponsor`,
`intervention_name`, `condition`):

**Phase 1 — discover.** Page the base filter with
`fields=NCTId|{dim.record_path}` for up to `SAMPLE_PAGES` (default 3) × `pageSize=1000`.
Serial paging, same rules as T10. Tally raw label frequencies. This gives candidate labels,
**not counts** — the sample is relevance-ranked (there is no default sort, notes §3), so its
frequencies are biased and must never be reported as values.

**Phase 2 — confirm.** Take the top `K = options.max_buckets` labels and issue one
count-only request per label, concurrently:
`Essie.full_match(dim.area, label)` ANDed with the base filter.

`COVERAGE[FullMatch]` is **mandatory** here, on every open dimension, without exception.
Without it, `AREA[LeadSponsorName]"Merck"` returns 2,733 — matching Merck KGaA and others —
where the correct exact count is 1,841 (notes §2, SPEC A2). This is the single most
important line in this task.

**Do not "optimize" it away on the strength of notes §6.7.** T06 measured that `FullMatch`
is a *no-op* on `LocationCountry` — `Niger` returns 47 with or without it, and `Nigeria`'s
454 is never absorbed — because country values are normalized exact terms. That is a fact
about `LocationCountry`, not a fact about open vocabularies as a category. The defect it
guards against is **free-text embedding**: `LeadSponsorName`, `InterventionName` and
`Condition` are unnormalized strings where a short name is a substring of longer ones
(notes §6.5: 51,610 distinct lead sponsors, "Novartis" vs "Novartis Pharmaceuticals"). T03
measured that over-escaping and over-scoping are harmless, so applying `FullMatch`
uniformly costs nothing and removing it selectively costs 892 studies on one sponsor.

**Phase 3 — disclose.** The resulting `BucketSet` carries:
- `exactness="exact"` on every bucket — each confirmed count *is* exact.
- `sample_size` = studies actually examined in phase 1.
- `sample_coverage` = `sample_size / total`, rounded to 3 dp.
- A warning with this exact structure:
  > `"Labels were discovered from a 3,000-study sample (5.2% of 57,400 matching studies).
  > Each displayed count is exact and confirmed against the full corpus; however, a label
  > appearing only outside the sample may be missing from this chart."`

That sentence is the whole point of the mode. Per-label counts are exact; the label **set**
may be incomplete (SPEC §4.3). Don't soften it, don't shorten it to "results may be
incomplete", and don't emit a bare `truncated: true`.

Also emit `unclassified_count` from `Essie.missing(dim.area)` as in every other mode.

Free-text caveats to disclose in `meta.assumptions` when the dimension is
`lead_sponsor`: sponsor names are free text — 51,610 distinct lead sponsors, "Novartis" vs
"Novartis Pharmaceuticals" are separate labels and are **not** merged (notes §6.5). Only
`sponsor_class` is reliably categorical; suggest it in the assumption text as the
alternative that *is* a clean partition.

Note the upstream caveat honestly (notes §2): *"COVERAGE and EXPANSION operators are not
fully implemented on the modernized ClinicalTrials.gov."* They execute, but treat as
approximate — one line in `meta.assumptions` for this mode saying exactly that.

## Budget interaction

Phase 1 (3 serial pages) + phase 2 (K concurrent counts) + 1 missing count ≈ `4 + K`
requests. With `max_buckets=20` that's 24, inside the 40-request budget (SPEC §7) — but
narrow enough that citations (T08) may need to be cut. Check `ctx.upstream_budget` **before**
phase 2 and reduce K if needed, reporting the reduced K in the warning with the original K.

## Tests

- **SPEC A3**: `total > 2000` on an open dimension → `aggregation_mode:
  "sampled_then_confirmed"`, `sample_size` and `sample_coverage` both populated and
  non-null, warning present. `pageSize` never exceeds 1000 in any logged URL.
- **SPEC A2**: a `lead_sponsor` chart reports **1841** for `"Merck Sharp & Dohme LLC"`, not
  2733. Assert the logged predicate contains `FullMatch`.
- A label discovered in the sample whose confirmed count is 0 → dropped from `data` with a
  warning naming it (it's a sampling or escaping artifact, and hiding it silently would mask
  a real escaping bug).
- Confirmed counts, not sample frequencies, appear in `data` — fixture where the sample
  frequency and the true count deliberately differ, and the test asserts the true count.
- `sample_coverage` arithmetic against a known `sample_size`/`total`.
- Budget pressure → K reduced, warning states both the reduced and the requested K.
- The disclosure sentence matches the template, including thousands separators and the
  percentage to one decimal place.

## If you cut this task

Above the threshold on an open dimension, return a `table` of the base filter's total plus a
warning explaining that per-label counts for an open vocabulary at this scale need the
sampling path, and suggest narrowing the filter or grouping by `sponsor_class` instead.
Honest and useful beats absent — but say so in the README's "what I'd do with more time".
