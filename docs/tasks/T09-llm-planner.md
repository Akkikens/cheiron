# T09 — LLM planner, repair loop, and plan cache

**Est. 30 min · depends on: T05, T07 · unblocks: T12**

SPEC §1, §3, §7. The model's entire job is question → `AnalysisPlan`. It never sees study
data, so it cannot invent a count.

## `app/planner/llm.py`

```python
class LLMPlanner:
    async def plan(self, req: AnalyzeRequest, vocab: Vocabulary) -> PlanResult
```

Uses the OpenAI Structured Outputs API with `strict: true` and the schema from
`AnalysisPlan.json_schema_strict()` (T04) — one source of truth for the shape, so the model
contract and the Pydantic model can never drift.

### Prompt construction

System prompt states, in this order:
1. The job: translate a question into an `AnalysisPlan`. Nothing else.
2. The prohibition, explicitly: **never** produce a count, a study total, a sponsor name you
   were not given, or any factual claim. `interpretation` describes *what will be
   computed*, in the future/descriptive tense — not what the answer is.
3. The **live vocabulary**, injected from `vocab` (SPEC §3: re-validated against
   `/studies/enums`; never hardcode enum lists into the prompt string).
4. The dimension registry keys with their partition flags, so the model doesn't propose a
   dimension the engine can't group by.
5. The structured hints from the request, flagged as **hard constraints it must not
   contradict** (SPEC §2.1). `enforce_hard_constraints` (T05) is the belt to this braces —
   run it regardless of what the model returns.

Few-shot: 3 examples, reusing the golden cases from T05's heuristic tests so the two
planners are anchored to the same targets. Include SPEC §3's worked example verbatim as one
of them.

`temperature=0`, `seed` pinned, `max_tokens` bounded. Model id from config
(`OPENAI_MODEL`, default `gpt-4.1`).

### Repair loop (SPEC §3)

```
attempt 1 → validate_plan() → ok? done, planner="llm"
          → errors? feed the error strings back verbatim → attempt 2
attempt 2 → ok? done, planner="llm_repaired"
attempt 3 → ok? done, planner="llm_repaired"
still bad → heuristic fallback, planner="heuristic_fallback", warning recorded
```

At most **2 repair attempts** (3 model calls total, matching SPEC §7's budget). The request
**never** fails because the model misbehaved — every terminal path lands on the heuristic
planner. If the heuristic also can't plan, *then* `422 unplannable_query`.

Also fall back to heuristic (not error) on: API timeout, rate limit, malformed response,
missing key with `LLM_ENABLED=true`. Each records a warning naming the cause. A model
outage degrades coverage, not availability (SPEC §5.5).

### Plan cache (SPEC §7)

`app/cache.py`: a `Cache` protocol plus a `TTLCache`-backed in-process implementation.

- **Plan cache** keyed on `(normalized question text, sorted structured fields)` → the plan.
  A repeat question skips the model entirely — this is the "it is cheap" property in SPEC
  §1's table, so make it observable: `meta.timing_ms.plan` near zero on a hit, and count
  cache hits in a counter surfaced at `/health`.
- **Result cache** keyed on `(plan.normalized_key(), data_timestamp)` (SPEC §7). Upstream
  refreshes weekdays ~14:00 UTC (notes intro), so a 24 h TTL is right, but the
  `data_timestamp` in the key is what actually guarantees correctness — the TTL is only
  housekeeping.
- Normalize question text for the plan key: strip, collapse whitespace, casefold. Do **not**
  stem or drop stopwords — `"trials in France"` and `"trials for France"` are the same query
  but proving that isn't worth a wrong cache hit.

## Tests (all mocked — no live OpenAI calls in the suite)

- A stubbed client returning a valid plan → `planner="llm"`, one model call.
- Returns an invalid enum, then valid → `planner="llm_repaired"`, two calls, and the repair
  message contains the offending value **and** the valid alternatives.
- Returns garbage 3× → `planner="heuristic_fallback"`, exactly 3 calls, warning recorded,
  **200 response** with a correct chart.
- Model contradicts a structured hint (`drug_name="Pembrolizumab"` but plan says
  `"aspirin"`) → hint wins, assumption recorded.
- Model tries to smuggle a number into `interpretation` (`"There are 2,927 trials"`) →
  validation rejects it (T05 rule 5), repair triggered.
- `LLM_ENABLED=false` with no key → `LLMPlanner` is never constructed; `/health` reports
  `llm_enabled: false`; template questions still work (SPEC A6).
- Plan cache: same question twice → one model call. Result cache: same plan twice → one
  upstream fan-out. Changed `data_timestamp` → result cache miss, plan cache **hit**.
- `options.explain=true` includes the plan in `meta`; the plan is otherwise absent.

## Guardrails

- Grep test: no study data, count, or record dict is ever interpolated into a prompt string.
  The only dynamic content in the prompt is the question, the structured hints, the
  vocabulary, and the dimension keys. This is the one test that proves SPEC §1's thesis
  holds in the implementation, so make it explicit and name it
  `test_no_study_data_reaches_the_model`.
- Log every model call's token counts. You are on API billing now — a runaway repair loop
  should be visible, and the ≤3-call budget makes the worst case bounded and provable.
