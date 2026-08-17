"""Vercel entry point. The whole service, as one ASGI function.

Vercel's Python runtime serves an ASGI `app` exported from a file under `api/`, and
`vercel.json` rewrites every path here, so `/`, `/health`, `/analyze` and `/docs` are the same
routes they are locally — this file adds no behaviour of its own.

**Serverless does not reliably run FastAPI's lifespan**, which is why `create_app` assigns the
transport and caches at construction and only warms the vocabulary in the startup hook. The
first request on a cold instance loads `/studies/enums` lazily and pays for it once; instances
kept warm reuse it for the six-hour TTL. Both caches are in-process, so they are per-instance
here rather than shared — correct but less effective than a single long-lived process, and
called out in the README's "with more time".
"""

from __future__ import annotations

from app.main import create_app

app = create_app()
