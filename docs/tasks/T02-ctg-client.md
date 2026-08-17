# T02: `CTGClient`: transport, courtesy, and the vocabulary loader

**Est. 25 min · depends on: T01 · unblocks: T03, T06**

Implements SPEC §7 "upstream courtesy" and every transport gotcha in
`CTG-API-NOTES.md` §4.

## `app/ctg/client.py`

```python
@dataclass(frozen=True)
class Version:
    api_version: str
    data_timestamp: str          # e.g. "2026-08-14T09:00:05"

@dataclass(frozen=True)
class StudyPage:
    studies: list[dict[str, Any]]
    next_page_token: str | None
    total_count: int | None      # only present when countTotal was sent

class CTGClient:
    async def count(self, params: Mapping[str, str]) -> int
    async def page(self, params: Mapping[str, str], page_token: str | None = None) -> StudyPage
    async def version(self) -> Version
    async def enums(self) -> dict[str, list[str]]
    @property
    def query_log(self) -> list[str]
```

Behaviour, all of it non-negotiable:

1. **`text/plain` errors.** Never call `response.json()` on a non-2xx. Dispatch on status +
   body string into `CheironError`: 4xx from a malformed predicate → `upstream_error` (502,
   because it's *our* predicate bug, and the message must include the offending
   `filter.advanced` value); 5xx → `upstream_error`; timeout → `upstream_timeout` (504).
2. **`count()`** always sends `countTotal=true&pageSize=1&fields=NCTId` and returns
   `totalCount`. Assert `totalCount` is present; a missing field is a bug, not a zero.
3. **`pageSize` clamp.** Reject `pageSize > 1000` client-side with an assertion. Upstream
   clamps silently (notes §3) and we must never depend on that.
4. **`pageToken` binding.** `page()` computes `sha256` of the sorted non-paging params and
   stores it alongside each issued token. Passing a token minted by a *different* param set
   raises `ValueError`: notes §3 warns this silently returns wrong data upstream.
5. **Courtesy:** `asyncio.Semaphore(MAX_CONCURRENCY)`; token bucket (default 8 req/s,
   burst 16); circuit breaker (5 consecutive failures → open 30 s → half-open single
   probe) raising `upstream_circuit_open` (503) with `retry_after_seconds`; retry on
   timeout/5xx/`429` only, 3 attempts, exponential backoff with full jitter. **Never retry
   a 4xx**: a bad predicate is deterministic.
6. **Transport:** one shared `httpx.AsyncClient(http2=True)`, `Accept-Encoding: gzip`,
   descriptive `User-Agent: cheiron/0.1 (+contact)`, connect 3 s / read 8 s timeouts.
7. **ETag revalidation** on `version()` and `enums()`: store the tag, send
   `If-None-Match`, serve cached body on `304`.
8. **`query_log`** records every fully-formed upstream URL in issue order. Feeds
   `meta.api_query_log` when `options.explain` (SPEC §4.3). One log per client instance
   construct a client (or a logging scope) per request.

## `app/ctg/vocab.py`

```python
class Vocabulary:
    @classmethod
    async def load(cls, client: CTGClient) -> Vocabulary
    def values(self, enum_name: str) -> tuple[str, ...]
    def is_valid(self, enum_name: str, value: str) -> bool
    def label(self, enum_name: str, value: str) -> str    # "PHASE2" -> "Phase 2"
    def sort_order(self, enum_name: str) -> tuple[str, ...]
```

- Loaded from `/studies/enums` at startup and cached with a 6-hour TTL, **never
  hardcoded** (SPEC §3, notes §7). The lists in notes §7 are reference only.
- Human labels: derived by a deterministic rule (`PHASE2`→`Phase 2`,
  `ACTIVE_NOT_RECRUITING`→`Active, not recruiting`) plus a small explicit override map for
  the ones the rule gets wrong: at minimum `NA`→`Not Applicable`, `NIH`→`NIH`. Labels are
  code, never model output (SPEC §4.1).
- `sort_order` returns the natural clinical order for `Phase`
  (`EARLY_PHASE1 < PHASE1 < … < PHASE4 < NA < MISSING`, matching SPEC §4's `sort` array),
  and upstream declaration order otherwise.
- Startup must not hard-fail if `/studies/enums` is unreachable: log, serve `/health` with
  `"vocabulary": "unavailable"`, and let `/analyze` return `502 upstream_error`.

## Tests (`httpx.MockTransport` via the injectable transport seam, no network)

- `text/plain` 400 body → `CheironError(upstream_error, 502)` whose message contains the
  predicate; `response.json()` is never called (assert via a body that isn't valid JSON).
- Token minted for params A, replayed with params B → `ValueError`.
- Breaker: 5 injected 500s → 6th call raises `upstream_circuit_open` without an HTTP
  attempt; after the window a single probe is allowed.
- `304` on `enums()` serves the cached payload and issues no re-parse.
- `pageSize=5000` raises before any request is made.
- `Phase` sort order matches SPEC §4 exactly.

## Done when

`scripts/verify_upstream.py` prints a live `ALL` count matching notes §2 (598,690 ± daily
drift) and the current `dataTimestamp`, and the unit suite is green.
