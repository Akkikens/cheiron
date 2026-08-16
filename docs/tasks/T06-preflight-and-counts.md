# T06 — Preflight, the `server_counts` fan-out, and coverage math

**Est. 30 min · depends on: T03, T05 · unblocks: T07, T11**

The heart of the system. SPEC §5.2, §5.3, §4.3; notes §1, §6.

## `app/engine/context.py`

```python
@dataclass
class RunContext:
    client: CTGClient
    vocab: Vocabulary
    options: Options
    deadline: float                  # monotonic; REQUEST_BUDGET_MS from now
    upstream_budget: int             # MAX_UPSTREAM_REQUESTS, decremented per call
    data_timestamp: str              # captured at preflight
    warnings: list[str]
    assumptions: list[str]

class DataTimestampChanged(Exception): ...
class BudgetExhausted(Exception): ...
```

`ctx.spend(n)` raises `BudgetExhausted` → `504 upstream_timeout` with a message stating the
budget and what it was spent on. Checking the deadline before each fan-out wave, not just
at the end.

## Base filter construction — `query.*` and `filter.advanced` are not interchangeable

T03 measured that the same free-text expression returns different counts depending on which
parameter carries it: `(head OR neck) AND pain NOT cancer` is **2,075** under `query.cond`
and **9,964** under `filter.advanced`; `"breast cancer"` is **16,538** vs **17,819**.
`query.*` params are scoped to a search area and participate in relevance ranking;
`filter.advanced` searches unscoped.

So the base filter has two halves and they must never be merged:

- **Free-text filters go to `query.*`**, per SPEC §2.1's mapping: `intervention` →
  `query.intr`, `condition` → `query.cond`, `sponsor` → `query.lead`, `term` → `query.term`.
- **Bucket predicates and structured constraints go to `filter.advanced`** via the Essie
  builder (§5.3).

This is not stylistic. SPEC A1's 2,927 total was measured with `query.intr=pembrolizumab`;
moving that term into `filter.advanced` as an apparent simplification changes the total and
silently invalidates every bucket beneath it. Add a test asserting `drug_name` lands in
`query.intr` and never in `filter.advanced`, and that A1's preflight URL carries exactly
that shape.

## `app/engine/preflight.py`

`async def preflight(plan, dim, ctx) -> Preflight` where
`Preflight = {total: int, mode: str, data_timestamp: str}`.

One request: base filter + `countTotal=true&pageSize=1&fields=NCTId` (~150 bytes). Then
SPEC §5.2's table:

| Condition | Mode |
|---|---|
| `total <= RECORD_MODE_THRESHOLD` (2000) | `complete_records` |
| `total > 2000` and `dim.enum_name is not None` | `server_counts` |
| `total > 2000` and open vocabulary | `sampled_then_confirmed` |

Record *why* the threshold exists in the module docstring: pageToken chains are strictly
serial (notes §1), so 2k studies ≈ 2 round trips but 50k ≈ 50 ≈ 25–50 s. This is the single
number a reviewer is most likely to ask about.

Until T10/T11 land, unimplemented modes raise a clear `NotImplementedError` that the route
converts to `unplannable_query` with an explanation — never a silent downgrade.

## `app/engine/modes/counts.py`

For a closed-vocabulary dimension:

1. Build one predicate per enum value via `Essie.field_eq(dim.area, value)` ANDed with the
   base filter. Values come from `vocab.values(dim.enum_name)` — never hardcoded.
2. Issue all of them **concurrently** through the client (the semaphore does the
   throttling), plus one `Essie.missing(dim.area)` for `unclassified_count`.
3. **Any bucket failure fails the whole group-by** (SPEC §4.5: "partial aggregations are
   never rendered"). Use `asyncio.gather(..., return_exceptions=True)` and re-raise the
   first exception after cancelling the rest — a chart with a silently missing bar is worse
   than an error.
4. Re-read `dataTimestamp` after the wave. If it changed, raise `DataTimestampChanged`;
   the caller retries the entire group-by **once**, then fails (SPEC §7).
5. Year dimensions use `Essie.date_range` per bucket, derived from
   `filters.start_year..end_year` (defaulting to the last 10 years when unset) at
   `bin.size` granularity. Cap buckets at `options.max_buckets` and report the clamp.
6. Return a `BucketSet` (BUILD-PLAN §4) with `exactness="exact"` on every bucket — count
   fan-out counts are exact regardless of result-set size.

## `app/engine/coverage.py`

`build_coverage(bucketset, dim) -> Coverage` implementing SPEC §4.3's rules:

- `groupby_semantics = "partition" if dim.partition else "overlapping"`.
- `bucket_sum = sum(b.value)`; `unclassified_count` always present.
- When overlapping, `overlap_note` **must state actual numbers**. Generate exactly SPEC
  §4.3's shape:
  > `"phases is multi-valued: 2,758 studies carry ≥1 phase and contribute 3,273 bucket
  > memberships (overlap 515). Bucket counts are each exact; they do not sum to the total."`
  Derive: `with_value = total - unclassified`, `memberships = bucket_sum`,
  `overlap = memberships - with_value`. Thousands separators. If `overlap == 0` say so
  explicitly rather than omitting the note.
- When `partition` and `bucket_sum + unclassified != total`, that is a **bug or an upstream
  surprise**, not a rounding issue: emit a warning naming both numbers. Do not paper over it.
- A bare `truncated: true` is forbidden anywhere in the output (SPEC §4.3). Add a test that
  greps the serialised response for the key `truncated` and fails.

## Tests

- **SPEC A1, against a recorded fixture** — `query.intr=pembrolizumab` grouped by phase:
  ```
  total 2927 · MISSING 169 · buckets NA 53 · EARLY_PHASE1 51 · PHASE1 1039 ·
  PHASE2 1750 · PHASE3 363 · PHASE4 17 · Σ 3273 · overlap 515
  ```
  Assert `bucket_sum == 3273`, `unclassified_count == 169`,
  `groupby_semantics == "overlapping"`, and **no** share/percentage key anywhere.
- `NA` and `MISSING` are distinct buckets and never merged (notes §6.1) — 234,433 vs
  141,903 corpus-wide.
- One injected bucket failure → whole group-by raises; no partial `BucketSet` escapes.
- `dataTimestamp` changing mid-fan-out → one retry, then `502`.
- Preflight mode selection at 1999 / 2000 / 2001 studies × closed/open vocabulary.
- `overlap_note` prose contains the literal computed integers, formatted with separators.

## Done when

SPEC A1 passes end-to-end at the engine layer, and one live run against
`query.intr=pembrolizumab` reproduces the numbers above (allowing for daily drift, which
the test tolerates only in the live script, never in the pinned test).
