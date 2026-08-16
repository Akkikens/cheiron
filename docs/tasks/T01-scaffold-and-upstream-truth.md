# T01 — Scaffold, config, errors, and one upstream truth to settle

**Est. 20 min · depends on: nothing · unblocks: everything**

## Part A — settle the operator name (do this first, before any code)

`docs/CTG-API-NOTES.md` §2 says `COVER[FullMatch]`. `SPEC.md` §5.3 and §8-A2 say
`COVERAGE[FullMatch]`. They cannot both be right and this operator is load-bearing for
every open-vocabulary count in the system.

Run all four live:

```bash
B=https://clinicaltrials.gov/api/v2/studies
Q='&countTotal=true&pageSize=1&fields=NCTId'
for OP in COVER COVERAGE; do
  for EXPR in 'AREA[LeadSponsorName]'$OP'[FullMatch]"Merck Sharp & Dohme LLC"' \
              'AREA[LeadSponsorName]'$OP'[FullMatch]Merck'; do
    printf '%s -> ' "$EXPR"
    curl -sG "$B" --data-urlencode "filter.advanced=$EXPR" --data-raw "${Q#&}" \
      | python3 -c 'import sys,json;d=sys.stdin.read();print(json.loads(d)["totalCount"] if d.startswith("{") else d[:120])'
  done
done
```

Expected: the correct spelling returns **1841** for the full name; the wrong one either
400s with `text/plain` or returns something else. Also confirm the un-operatored baseline
`AREA[LeadSponsorName]"Merck"` → **2733**.

Then:
- Fix the wrong spelling in **both** `SPEC.md` and `docs/CTG-API-NOTES.md`.
- Add the exact commands and counts you observed to `CTG-API-NOTES.md` §2 as a verified row.
- Record the raw JSON responses to `tests/fixtures/upstream/fullmatch_*.json`.
- Delete item 1 from `docs/BUILD-PLAN.md` §6.

If both spellings work identically, say so in the notes and pick `COVERAGE` (matches the
official caveat wording in §2). Do not leave the ambiguity in the repo.

## Part B — scaffold

Create:

- `pyproject.toml` — deps per BUILD-PLAN §1; ruff (line length 100) + mypy (`strict` for
  `app.*`) + pytest (`asyncio_mode = "auto"`) config.
- `.env.example` — every var in BUILD-PLAN §1 with safe defaults and `OPENAI_API_KEY=`
  left blank. Commit this one (`.gitignore` already un-ignores it).
- `app/config.py` — `Settings(BaseSettings)`, `env_file=".env"`, module-level
  `get_settings()` memoised with `functools.lru_cache`.
- `app/errors.py`:
  ```python
  class ErrorCode(StrEnum):
      INVALID_REQUEST = "invalid_request"
      UNPLANNABLE_QUERY = "unplannable_query"
      UPSTREAM_ERROR = "upstream_error"
      UPSTREAM_TIMEOUT = "upstream_timeout"
      UPSTREAM_CIRCUIT_OPEN = "upstream_circuit_open"
      RATE_LIMITED = "rate_limited"

  class CheironError(Exception):
      code: ErrorCode
      status: int
      message: str
      details: list[dict[str, Any]]
      retry_after_seconds: int | None
  ```
  Plus FastAPI handlers rendering exactly SPEC §4.5's envelope
  (`{"error": {code, message, request_id, retry_after_seconds?, details?}}`).
  `request_id` is a per-request uuid4 hex set on `request.state` by middleware and echoed
  in an `X-Request-Id` response header.
- `app/main.py` — `create_app()` factory, handlers registered, `GET /health` returning
  `{"status": "ok", "llm_enabled": bool}`. No `/analyze` yet.
- `tests/unit/test_errors.py` — each `CheironError` maps to its documented status and the
  envelope shape matches SPEC §4.5. Assert a Pydantic `ValidationError` on the request
  model surfaces as `422 invalid_request` with a non-empty `details[]` (stub the model if
  T04 hasn't landed).

## Done when

- `uvicorn app.main:create_app --factory` serves `GET /health`.
- `ruff check . && ruff format --check . && mypy app && pytest -q` is green.
- `SPEC.md` and `CTG-API-NOTES.md` agree on the FullMatch operator, with a live-verified
  count recorded.

## Guardrails

- No `/analyze` route, no CTG client, no models beyond what the error test needs.
- `Settings` must construct successfully with **no** `OPENAI_API_KEY` when
  `LLM_ENABLED=false`. Add a validator asserting the key is present only when enabled.
