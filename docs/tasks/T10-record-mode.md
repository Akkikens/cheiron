# T10 — `complete_records` mode and the network graph

**Est. 25 min · depends on: T07 · cuttable: first, if behind schedule**

SPEC §5.2, §5.4; notes §1, §3, §5. This mode is where the system is at its best: exact,
unbiased, all dimensions at once, citations free.

## `app/engine/modes/records.py`

For `total <= 2000`:

1. Page `/studies` with a `fields=` projection covering **every** dimension's
   `record_path` plus `NCTId` — one pass serves any group-by, so a secondary group-by costs
   nothing extra. A projected study is ~550 B vs 17.3 KB full (notes §3).
2. **Pages are strictly serial** — each needs the previous `nextPageToken` (notes §1).
   `pageSize=1000` (never higher; the clamp is silent), so ≤2 pages, ~1 s. Every subsequent
   page must repeat every parameter except `countTotal`/`pageSize`/`pageToken` (notes §3),
   and the token binding from T02 will catch it if you don't.
3. Aggregate in-process into a `BucketSet`. For `is_list` dimensions a study contributes to
   **every** value it carries — that's the overlap SPEC A1 quantifies, and now you can
   compute it directly rather than inferring it.
4. Handle the notes §6 data-quality traps explicitly, each with a test:
   - `phases: ["NA"]` (explicit) vs no `phases` key (missing) → **two distinct buckets**,
     never merged.
   - Mixed date precision `yyyy-MM` and `yyyy-MM-dd` → parse both; a missing start date is
     `unclassified`, not year 1900.
   - Enrollment: missing on some studies, `99999999` placeholders, max 188,814,085. For
     `enrollment_sum`/`enrollment_median`, **winsorize at the 99th percentile** and state it
     in `meta.assumptions` with the clamp value and how many studies were clamped. Never
     plot raw enrollment.
   - Very old records (e.g. NCT00000102) have empty `statusModule`/`designModule` — every
     path access is defensive, and absent fields land in `unclassified`.
5. `enrollment_sum` / `enrollment_median` are computable **only** here. Above the threshold
   they must fail with `unplannable_query` and a suggestion to narrow the filter — not
   degrade silently to `study_count` (BUILD-PLAN §6.3).
6. Citations are free (T08): sample from the in-memory records, zero extra requests.

## `app/engine/modes/network.py` — SPEC §5.4

Available **only** in `complete_records` mode, where co-occurrence is computed from the full
result set and is therefore exact and unbiased. Outside that regime: downgrade to
`grouped_bar_chart` and record the reason and the observed total in `meta.warnings`
(SPEC §6.1 safety rule 3, A7).

Sampled co-occurrence is **not offered at all** — a network graph built from a
relevance-ranked sample looks authoritative and isn't. Don't add a flag for it.

Three node/edge pairings:
- `sponsor ↔ intervention`
- `intervention ↔ intervention` (co-occurring in the same trial)
- `condition ↔ intervention`

Output per SPEC §6.2: `{"nodes": [{id, label, group, weight}], "edges": [{source, target,
weight}]}` where edge `weight` is the number of co-occurring trials. Edges carry citations
(the NCT ids of co-occurring trials, capped at `citations_per_datum`).

Pruning: cap nodes at `max_buckets` by degree-weighted rank and drop edges with
`weight < 2` by default. **Report both** in an annotation with actual numbers — `"showing
20 of 143 nodes and 88 of 611 edges; edges with a single co-occurring trial are hidden"`.
No bare `truncated: true` (SPEC §4.3).

## Tests

- 2,400-study fixture (2 pages + a third empty) aggregates identically to what the
  `server_counts` fan-out would produce for the same closed dimension. **This is the key
  test**: two independent implementations agreeing is real evidence the aggregation is right.
- Serial paging: page 2's params match page 1's exactly except the paging trio; a mismatched
  token raises.
- Explicit `NA` vs absent `phases` → distinct buckets.
- `yyyy-MM` and `yyyy-MM-dd` both bucket to the right year; absent date → unclassified.
- Enrollment winsorizing: a fixture containing `99999999` and `188814085` produces a
  documented clamp, a stated clamp count, and a plausible median.
- A record with empty `designModule` doesn't raise and lands in `unclassified`.
- `intent=network` with `total=2400` → `network_graph`; with `total=25000` →
  `grouped_bar_chart` + warning naming the total (A7).
- Network on a fixture with known co-occurrences → exact expected edge weights.
- Citations in record mode add zero upstream requests.
