# Cheiron — 4-Hour Build Plan

Companion to [`../SPEC.md`](../SPEC.md) (the contract) and
[`CTG-API-NOTES.md`](CTG-API-NOTES.md) (verified upstream behaviour).

This file is the **build order**: stack decisions, module layout, and the shared
preamble for every task. Individual task specs live in [`tasks/`](tasks/) and are written
to be pasted into Cursor one at a time, in order.

---

## 0. Ground rules for the agent

Paste this preamble with **every** task prompt:

> You are implementing Cheiron, described in `SPEC.md` (authoritative contract) and
> `docs/CTG-API-NOTES.md` (verified upstream facts). Read both before writing code.
>
> Hard rules:
> 1. **The LLM never emits a number, label, or fact.** It emits only an `AnalysisPlan`.
>    If you find yourself passing study data into a prompt, stop — you've broken the thesis.
> 2. **Never use `aggFilters`.** All bucket predicates go through the Essie builder.
> 3. **Never invent upstream behaviour.** If `CTG-API-NOTES.md` doesn't cover it, either
>    verify with a live `curl` and add a row to the notes, or raise it as an open question.
>    Do not guess and do not add code paths for guessed behaviour.
> 4. **No silent truncation, anywhere.** Every cap, sample, or rollup must be reported in
>    `meta.coverage` or `meta.warnings` with actual numbers.
> 5. Only implement the task in front of you. Do not scaffold future modules, do not add
>    speculative config flags, do not refactor code from earlier tasks unless the task says to.
> 6. Every task ends green: `ruff check . && ruff format --check . && mypy app && pytest -q`.
> 7. Tests never hit the network. Live calls belong in `scripts/`, recorded into fixtures.

---

## 1. Stack (decided — do not re-litigate mid-build)

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Take-home is backend; `.gitignore` already assumes Python |
| Framework | FastAPI + Uvicorn | Async (the fan-out is I/O-bound), free OpenAPI docs |
| Models/validation | Pydantic v2 | `extra="forbid"` gives §2.1's "unknown fields are rejected" for free |
| HTTP client | `httpx.AsyncClient` (HTTP/2, gzip) | Concurrent count fan-out |
| LLM | OpenAI `gpt-4.1` via Structured Outputs (`strict: true`) | Key is already in `.env`; strict schema is §3's requirement |
| Cache | In-process `cachetools.TTLCache` behind a `Cache` protocol | Stateless-per-§7 means no shared store is required for the take-home |
| Tests | pytest + pytest-asyncio + `httpx.MockTransport` | T02 built an injectable transport seam; `respx` would patch around it. One mocking style only — `respx` is **not** a dependency |
| Lint/types | ruff + mypy (`strict` on `app/`) | |
| Deps | `uv` if available, else `pip` + `requirements.txt` | |

Config via `pydantic-settings` reading `.env`. Required env: `OPENAI_API_KEY`.
Feature flags: `LLM_ENABLED` (default `true`), `CTG_BASE_URL`, `REQUEST_BUDGET_MS=10000`,
`MAX_UPSTREAM_REQUESTS=40`, `MAX_CONCURRENCY=8`, `RECORD_MODE_THRESHOLD=2000`.

`LLM_ENABLED=false` must work with **no API key present at all** (SPEC §5.5, A6).

---

## 2. Module layout

```
app/
  main.py               FastAPI app; POST /analyze, GET /health
  config.py             Settings (pydantic-settings)
  errors.py             CheironError + code enum + FastAPI exception handlers
  models/
    request.py          AnalyzeRequest, Options
    plan.py             AnalysisPlan, StudyFilter, GroupBy, SeriesSpec, Intent, Metric
    response.py         AnalyzeResponse, Visualization, Encoding, Citation, Meta, Coverage
  ctg/
    client.py           CTGClient: transport, limiter, breaker, retry, ETag, text/plain errors
    essie.py            EssieBuilder: predicates + escaping
    vocab.py            Vocabulary: /studies/enums + /version loaders, cached
  planner/
    base.py             Planner protocol; PlanResult(plan, planner_name)
    heuristic.py        Rule-based fallback planner (§5.5)
    llm.py              OpenAI structured-output planner + repair loop
    validate.py         Plan coherence validation (beyond JSON Schema)
  engine/
    dimensions.py       Dimension registry (§5.1)
    preflight.py        Mode decision (§5.2)
    modes/
      counts.py         server_counts fan-out
      records.py        complete_records paging + in-process aggregation
      sampled.py        sampled_then_confirmed
    citations.py        Citation sampling (§4.2)
    coverage.py         Coverage math + overlap_note prose
  render/
    registry.py         Chart selection + non-overridable safety rules (§6.1)
    encode.py           Per-type encoding + title/subtitle (§6.2)
scripts/
  verify_upstream.py    Live curl-equivalents; refreshes docs + fixtures
  record_fixtures.py    Writes tests/fixtures/*.json from live calls
tests/
  fixtures/             Recorded upstream responses, pinned dataTimestamp
  unit/                 Per-module
  acceptance/           A1–A7 from SPEC §8
```

---

## 3. Task order and schedule

Wall-clock estimates assume Cursor doing the typing and you reviewing. **Stop at the
4-hour mark wherever you are** — the order is chosen so that every prefix is a coherent,
demoable system.

| # | Task | Est. | Depends on | Demoable after |
|---|---|---|---|---|
| [T01](tasks/T01-scaffold-and-upstream-truth.md) | Scaffold, config, errors, `/health`, **resolve `COVER` vs `COVERAGE`** | 20m | — | `GET /health` |
| [T02](tasks/T02-ctg-client.md) | `CTGClient` transport + limiter + breaker + `Vocabulary` | 25m | T01 | live count against CTG |
| [T03](tasks/T03-essie-and-dimensions.md) | Essie builder + escaping + dimension registry | 20m | T02 | predicates provably correct |
| [T04](tasks/T04-models.md) | Request/plan/response models | 20m | T01 | `422` on bad input |
| [T05](tasks/T05-heuristic-planner.md) | Heuristic planner + plan validation | 25m | T03, T04 | plan for 5 template questions |
| [T06](tasks/T06-preflight-and-counts.md) | Preflight + `server_counts` fan-out + coverage math | 30m | T03, T05 | **A1 reconciliation passes** |
| [T07](tasks/T07-render.md) | Chart registry + encoding + `/analyze` wired end-to-end | 30m | T06 | **first real bar chart, no LLM** |
| [T08](tasks/T08-citations.md) | Citations | 20m | T07 | citations on every datum |
| [T09](tasks/T09-llm-planner.md) | LLM planner + repair loop + plan cache | 30m | T05, T07 | natural-language questions |
| [T10](tasks/T10-record-mode.md) | `complete_records` mode + network graph | 25m | T07 | network graph, ≤2000 |
| [T11](tasks/T11-sampled-mode.md) | `sampled_then_confirmed` mode | 25m | T06 | **A3 passes** |
| [T12](tasks/T12-acceptance-and-readme.md) | A1–A7 acceptance suite + README + polish | 30m | all | the submission |

**If you fall behind, cut in this order:** T10 (network graph — SPEC already permits
downgrading), then T11 (fall back to `table` + a warning above threshold), then T08
(citations are the stated "bonus"). Never cut T12.

---

## 4. Cross-task contracts

Freeze these signatures in T01–T04 so later tasks don't renegotiate them.

```python
# ctg/client.py
class CTGClient:
    async def count(self, params: Mapping[str, str]) -> int: ...
    async def page(self, params: Mapping[str, str], page_token: str | None) -> StudyPage: ...
    async def version(self) -> Version: ...          # {api_version, data_timestamp}
    async def enums(self) -> dict[str, list[str]]: ...
    @property
    def query_log(self) -> list[str]: ...            # feeds meta.api_query_log

# engine/modes/*.py — every mode satisfies this
class AggregationMode(Protocol):
    name: Literal["complete_records", "server_counts", "sampled_then_confirmed"]
    async def run(self, plan: AnalysisPlan, dim: Dimension, ctx: RunContext) -> BucketSet: ...

# BucketSet is the single handoff into render/
@dataclass(frozen=True)
class Bucket:
    key: str
    label: str
    value: float
    exactness: Literal["exact", "estimated"]
    citations: list[Citation]

@dataclass(frozen=True)
class BucketSet:
    buckets: list[Bucket]
    total: int
    unclassified: int
    semantics: Literal["partition", "overlapping"]
    mode: str
    sample_size: int | None
    sample_coverage: float | None
    warnings: list[str]
```

`RunContext` carries the client, vocabulary, request budget (deadline + remaining upstream
request allowance), and the `data_timestamp` captured at preflight. Any mode that observes
a changed `data_timestamp` raises `DataTimestampChanged` → the whole group-by retries once
(SPEC §7).

---

## 5. Definition of done for the submission

- `POST /analyze` answers all of SPEC §8's questions with correct, cited numbers.
- `LLM_ENABLED=false` with no `OPENAI_API_KEY` still serves the template questions (A6).
- A1–A7 pass against pinned fixtures, in CI-able form (`pytest -q`).
- `meta.coverage` is populated on every response; no response contains a bare
  `truncated: true`.
- README: one-command run, one-command test, three example `curl`s with real output, and
  an honest "what I'd do with more time" section.

---

## 6. Open questions (resolve in T01, then delete this section)

> **Resolved in T01 — `COVER` vs `COVERAGE`.** Both spellings are exact grammar aliases and
> both return 1,841; the codebase standardises on `COVERAGE`. `CTG-API-NOTES.md` §2 now
> carries the live-verified table, including the finding that omitting the `AREA[]` prefix
> returns 4,591 at HTTP 200.

1. **`sponsor` → `query.lead`** is settled (SPEC §2.1) and disclosed in
   `meta.assumptions`. No further discussion.
2. **Enrollment metrics** (`enrollment_sum`, `enrollment_median`) cannot be computed by
   count fan-out — they need record mode. Until T10 lands, a plan with an enrollment metric
   above the record-mode threshold must fail with `unplannable_query` and a suggestion,
   not silently degrade to `study_count`.
