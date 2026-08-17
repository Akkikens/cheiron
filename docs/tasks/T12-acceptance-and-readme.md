# T12: Acceptance suite, README, submission polish

**Est. 30 min · depends on: everything · never cut this**

SPEC §8's criteria are stated as executable tests pinned to a recorded `dataTimestamp`.
Make them literally executable.

## `tests/acceptance/`: one file per criterion

Each test names the criterion in its docstring and asserts on the **HTTP response**, not on
internals: these are contract tests.

| Test | Asserts |
|---|---|
| `test_a1_reconciliation.py` | `query.intr=pembrolizumab` by phase: `bucket_sum: 3273`, `unclassified_count: 169`, `groupby_semantics: "overlapping"`, buckets `NA 53 / EARLY_PHASE1 51 / PHASE1 1039 / PHASE2 1750 / PHASE3 363 / PHASE4 17`, overlap 515, and **no** share/percentage key anywhere in the payload |
| `test_a2_exact_match.py` | Sponsor bar chart reports **1841**, not 2733; logged predicate contains `FullMatch` |
| `test_a3_no_silent_truncation.py` | `total > 2000` + open dim → `sampled_then_confirmed`, `sample_coverage` populated, warning present; **no logged URL has `pageSize > 1000`** |
| `test_a4_no_fabrication.py` | Zero-match query → `data: []`, `total_matching_studies: 0`, warning; no invented row |
| `test_a5_planner_determinism.py` | Golden set of ~20 questions → stable `AnalysisPlan`; asserts the **IR**, not prose. Run each twice |
| `test_a6_degraded_mode.py` | `LLM_ENABLED=false`, `OPENAI_API_KEY` deleted from the env → the fallback-covered questions still return valid correct specs |
| `test_a7_safety_rules.py` | `intent=network` + `total > 2000` → downgrades and warns; `viz_hint="pie_chart"` on a non-partition dim → discarded |

Global invariants, one test each, run over **every** acceptance response:
- The payload contains no key named `truncated` (SPEC §4.3).
- `meta.coverage` is fully populated: all seven fields present.
- The response validates against the T04 response models.
- Every number in `data` is traceable: either a citation is present or
  `include_citations=false` was set.

## Fixtures

`scripts/record_fixtures.py` records the live responses A1–A7 need into
`tests/fixtures/upstream/`, stamped with the `dataTimestamp` they were captured at. Commit
them. The suite must pass offline, forever, on a plane. If a fixture's `dataTimestamp`
differs from the one the test expects, the test **fails loudly** with instructions to re-run
the recorder: a silently re-recorded fixture is how acceptance suites rot.

## README.md

Replace the current two-liner. Sections, in this order:

1. **What it does**: one paragraph, then SPEC §1's thesis quoted verbatim. Lead with the
   design decision, not the framework list.
2. **Run it**: `cp .env.example .env`, add key, `uv sync`, `uvicorn app.main:create_app
   --factory --reload`. Then the same three commands with `LLM_ENABLED=false` and no key, to
   show it works without one.
3. **Three worked examples**: real `curl` in, real (trimmed) JSON out, captured from a live
   run. One `bar_chart` with the A1 numbers, one `time_series`, one showing `meta.coverage`
   with a populated `overlap_note`. Real output, not illustrative output.
4. **How it works**: the pipeline line from SPEC §5, the three aggregation modes and the
   2000-study threshold with its reason (serial pageToken chains), and a link to
   `CTG-API-NOTES.md`.
5. **The interesting problems**: the things a reviewer should notice, each in 2–3 lines:
   `NA` ≠ missing; bucket sums legitimately exceed the total by ~12% and the API says so in
   numbers; `aggFilters=phase:na` returns 0 silently, which is why Essie is used everywhere;
   `COVERAGE[FullMatch]` turns 2,733 into the correct 1,841; the LLM is kept out of the data
   path entirely.

   Lead with the one finding that generalises: **identical syntax, different scope, plausible
   wrong number**: the failure mode this API punishes. Three instances, all caught by
   verifying against live counts rather than reading docs: a missing `AREA[]` prefix returns
   4,591 instead of 1,841 at HTTP 200; the same boolean expression returns 2,075 under
   `query.cond` and 9,964 under `filter.advanced`; `AREA[LeadSponsorName]"Merck"` substring-
   matches to 2,733. None of the three errors, and none is visible in the response.

   Then the `UNKNOWN` cohort, as the payoff for reading the data: 95,740 studies (16%) are
   `UNKNOWN`, and `LastKnownStatus` is populated for exactly that cohort and nobody else
   (`MISSING` = 502,950 of 598,690; `UNKNOWN AND NOT MISSING` = 95,740, so the overlap is
   total). Their last self-reported state was `RECRUITING` 54,030, `NOT_YET_RECRUITING`
   23,607, `ACTIVE_NOT_RECRUITING` 14,458, and `COMPLETED` **zero**, because a completed
   trial has no reason to lapse. The 16% every dashboard drops is dominated by trials that
   were recruiting or hadn't started when they went dark.
6. **Tests**: `pytest -q`, what the acceptance suite pins, and how to re-record fixtures.
7. **What I'd do with more time**: honest and specific. Anything cut per BUILD-PLAN §3,
   plus: an `enrollment_count` dimension, which is the single change that unlocks SPEC §6.1's
   `histogram` and `scatter_plot` rows: currently unreachable because §5.1's registry
   defines no quantitative dimension, and enrollment needs record-mode paging plus
   winsorizing to be plottable at all (notes §6.4: missing on 7,133, `99999999` placeholders,
   max 188,814,085); a shared cache for horizontal scale, `postFilter` for ranking-aware series,
   condition-hierarchy grouping (MeSH), and the `[unverified]` items in notes §4
   (rate limits, pageToken expiry).

## Final polish pass

- `ruff check . && ruff format --check . && mypy app && pytest -q`: all green, committed.
- Grep the repo for `aggFilters` → zero hits outside the docs' explanation of why it's
  avoided.
- Grep for `truncated` → zero hits in response-producing code.
- Grep for hardcoded enum lists → zero; all vocabulary loads from `/studies/enums`.
- Confirm `SPEC.md` matches the built behaviour. **If the code and SPEC disagree, fix
  whichever is wrong and say which in the commit message**: an accurate spec is part of the
  deliverable.
- Delete `docs/BUILD-PLAN.md` §6 if all its questions are resolved, or move the survivors
  into the README's "with more time".
- One commit per task, message naming the SPEC section implemented. The commit log should
  read as the build narrative.
