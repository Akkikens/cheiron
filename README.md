# cheiron

Take-Home Assignment - Backend Engineer Applied

A natural-language question about clinical trials in; a renderable, fully cited
visualization specification out. [`SPEC.md`](SPEC.md) is the authoritative contract;
[`docs/CTG-API-NOTES.md`](docs/CTG-API-NOTES.md) is the verified upstream behaviour it
depends on; [`docs/BUILD-PLAN.md`](docs/BUILD-PLAN.md) is the build order.

## Run

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
cp .env.example .env          # OPENAI_API_KEY only needed when LLM_ENABLED=true
.venv/bin/uvicorn app.main:create_app --factory
```

```bash
curl -s localhost:8000/health     # {"status":"ok","llm_enabled":true}
```

## Test

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . \
  && .venv/bin/mypy app && .venv/bin/pytest -q
```

Tests never touch the network. Live upstream responses are recorded into
`tests/fixtures/upstream/`.

`POST /analyze` is not wired yet — see `docs/BUILD-PLAN.md` §3 for what lands when.
