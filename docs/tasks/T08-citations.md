# T08: Citations

**Est. 20 min · depends on: T07 · unblocks: nothing (stated bonus)**

SPEC §4.2. The value here is entirely in the discipline, not the plumbing: a citation that
points at the wrong field is worse than no citation, because it looks rigorous.

## `app/engine/citations.py`

```python
async def sample_citations(
    bucket_predicate: str, dim: Dimension, n: int, ctx: RunContext
) -> tuple[list[Citation], str]      # (citations, citation_note)
```

Rules, each of which maps to a numbered rule in SPEC §4.2:

1. **`excerpt` is a verbatim serialization of the upstream response**: an exact substring
   or `json.dumps` of the actual value at `dim.record_path`. Never model-generated, never
   paraphrased, never reformatted. Assert this in a test by checking the excerpt appears in
   the raw fixture bytes (for scalars) or parses back equal (for lists).
2. **`field` is the structured field that determines bucket membership**: always
   `dim.record_path` (e.g. `protocolSection.designModule.phases`), never a `briefSummary`
   grep. This is the rule that matters most: `query.intr=keytruda` matches via upstream
   synonym expansion, so the literal query string may appear nowhere in the record. Citing
   `interventions[].name` is defensible; citing a summary scan is not. Add a test asserting
   no citation `field` ever contains `briefSummary` or `detailedDescription`.
3. **Citations are a sample and say so.** `citation_note` reads
   `"3 of 1,750 contributing studies"`: exact wording from SPEC §4.2, with separators.
   When the bucket has ≤ n studies, the note reads `"all 4 contributing studies"`.
4. `url` is `https://clinicaltrials.gov/study/{nct_id}`: construct it, don't take it from
   upstream.

Fetching:

- One request per bucket: bucket predicate + `pageSize=n` +
  `fields=NCTId|{dim.record_path}` (~550 B per study per notes §3), issued concurrently
  with the count fan-out wave, not after it.
- Skip entirely when `options.include_citations is False` or `citations_per_datum == 0`
 , and then don't spend the upstream budget either.
- **Scope this task to the `server_counts` path only.** The free-citations case belongs to
  `complete_records`, which doesn't exist until T10: building it now means stubbing the
  mode, and T10 already carries the obligation ("citations are free: sample from the
  in-memory records, zero extra requests", with the request-count test). Design
  `sample_citations` so the record-mode caller can pass already-fetched records instead of
  triggering a fetch, then leave that path to T10. Do not stub it here.
- Citation failure is **not** fatal (unlike a count failure): drop the citations for that
  bucket, add a warning naming the bucket. Evidence is nice-to-have; numbers are not.
- Citation requests count against `ctx.upstream_budget` and are the **first thing cut**
  when the budget or deadline is tight: degrade to fewer citations, record a warning, keep
  the numbers.

## Sampling determinism

Take the first `n` studies returned by the projected page. Sorting is not available on text
fields (notes §3: text sorts 400) and there is **no default sort** upstream, so ordering is
not guaranteed stable across calls. Say this in `meta.assumptions`:
`"Citations are the first N studies returned by the upstream API; ordering is not
guaranteed stable across requests."` Do not pretend to a determinism the upstream doesn't
offer.

## Tests

- Every datum in an A1-shaped response has ≤ `citations_per_datum` citations, each with all
  four fields populated.
- `excerpt` for a `PHASE2` bucket citation is the verbatim `["PHASE1","PHASE2"]`
  serialization from the fixture.
- No citation `field` references a free-text field.
- `citation_note` wording matches SPEC §4.2 exactly, both the sampled and the "all" case.
- `include_citations=false` → no `citations` key **and** zero citation requests in
  `query_log`.
- (Deferred to T10: `complete_records` citations present with upstream request count
  unchanged from the no-citations run.)
- An injected citation-fetch failure → numbers intact, warning present, no exception.
