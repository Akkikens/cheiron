# T05 — Heuristic planner and plan validation

**Est. 25 min · depends on: T03, T04 · unblocks: T06, T09**

Deliberately built **before** the LLM planner. It is the deterministic reference
implementation every LLM path is tested against (SPEC §5.5), and it makes the service
demoable with no API key.

## `app/planner/base.py`

```python
@dataclass(frozen=True)
class PlanResult:
    plan: AnalysisPlan
    planner: Literal["llm", "llm_repaired", "heuristic_fallback"]
    attempts: int

class Planner(Protocol):
    async def plan(self, req: AnalyzeRequest, vocab: Vocabulary) -> PlanResult: ...
```

## `app/planner/validate.py`

`validate_plan(plan, vocab) -> list[str]` returning **human-readable** error strings — they
are fed verbatim back to the model as repair input in T09, so they must say what is wrong
and what would be valid.

Checks:
1. `group_by.dimension` and `secondary_group_by.dimension` ∈ `dimensions.REGISTRY`.
2. Every enum value in `filters` and in each `series[].filters` is live-valid per `vocab`.
3. Coherence, per SPEC §3: `intent=network` requires `metric=study_count`;
   `intent=trend` requires a temporal `group_by` (`start_year`); `intent=geo` requires
   `group_by.dimension == "country"`; `intent=comparison` requires `len(series) >= 2`;
   `len(series) >= 2` requires `intent=comparison`; `intent=scatter` requires
   `secondary_group_by`; `bin` only on temporal or quantitative dimensions.
4. `start_year <= end_year`; both within 1900–2100.
5. `interpretation` non-empty, ≤300 chars, and contains **no digits that aren't a year** —
   cheap guard against the model smuggling a count into prose (SPEC §1).
6. Enrollment metrics: `metric != study_count` requires a plan the engine can actually
   serve. Until T10 lands, flag it (see BUILD-PLAN §6.3).

`enforce_hard_constraints(plan, req) -> AnalysisPlan` overwrites plan filters with the
request's structured fields wherever the request set one (SPEC §2.1: structured fields are
hard constraints that **override** anything inferred, and the model cannot contradict
them). This runs on every plan from every planner, LLM or not, and returns the list of
overrides it made for `meta.assumptions`.

## `app/planner/heuristic.py`

Rule-based, no model, no network beyond the vocabulary. Five template intents per SPEC
§5.5, matched by keyword over `query.lower()` plus the structured fields:

| Trigger keywords | Plan |
|---|---|
| `phase`, `phases` | `intent=distribution`, `group_by=phase` |
| `status`, `recruiting`, `completed`, `active` | `intent=distribution`, `group_by=overall_status` |
| `over time`, `trend`, `by year`, `since`, `changed`, `growth` | `intent=trend`, `group_by=start_year`, `bin.size=1` |
| `country`, `countries`, `where`, `geograph`, `region` | `intent=geo`, `group_by=country` |
| `sponsor`, `company`, `who is running`, `funder` | `intent=distribution`, `group_by=lead_sponsor` |

Resolution rules:
- First match in the table order above wins; ties are impossible by construction. Document
  the precedence in a module docstring — it is observable behaviour.
- No match → `CheironError(unplannable_query, 422)` with `suggestions` listing the five
  question shapes it *can* answer, phrased as examples the caller can retry with.
- Filters come **only** from the request's structured fields plus a conservative
  free-text mapping: if `drug_name` is absent, do not try to extract a drug name from the
  question — that's the LLM's job, and guessing it here would produce confidently wrong
  filters. Leave `filters.term = None`.
- `interpretation` is built from a format string over the resolved filters. Deterministic
  prose, never model prose.
- `viz_hint` is left `None`. Chart choice belongs to the registry (SPEC §6.1).

## Tests

- A golden table of ~15 `(question, structured fields) -> expected AnalysisPlan` cases
  asserting the **whole plan object**, not prose (SPEC A5).
- Same question twice → identical plan (determinism).
- `"What is the airspeed velocity of an unladen swallow?"` → `unplannable_query` with
  non-empty suggestions.
- `enforce_hard_constraints`: a plan with `filters.intervention="aspirin"` plus a request
  with `drug_name="Pembrolizumab"` yields `intervention="Pembrolizumab"` and one recorded
  assumption.
- Every validation rule in §3 above has one failing and one passing case.
- Every `validate_plan` message names the offending field and a valid alternative.

## Done when

The five template questions produce valid plans with `LLM_ENABLED=false` and no
`OPENAI_API_KEY` in the environment (this is half of SPEC A6).
