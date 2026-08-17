from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.analyze import analyze
from app.cache import RESULT_TTL_SECONDS, TTLStore
from app.config import Settings, get_settings
from app.ctg.client import CTGClient, CTGTransport
from app.ctg.vocab import VocabularyCache
from app.errors import install_error_handlers, install_request_id_middleware
from app.models.request import AnalyzeRequest
from app.models.response import AnalyzeResponse
from app.planner.llm import CachedPlan, ChatCompleter, openai_completer


def create_app(
    settings: Settings | None = None,
    *,
    transport: CTGTransport | None = None,
    completer: ChatCompleter | None = None,
) -> FastAPI:
    resolved = settings or get_settings()

    # Constructed only when the model is in play: with LLM_ENABLED=false the OpenAI SDK is never
    # imported and no key is read (SPEC A6).
    resolved_completer = completer
    if resolved_completer is None and resolved.llm_enabled:
        resolved_completer = openai_completer(resolved)

    owned = transport or CTGTransport(resolved)
    vocabulary_cache = VocabularyCache()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Warming only. Everything the routes need is assigned below, at construction, because
        # a serverless runtime may never run lifespan at all, and a service whose requests 500
        # unless a startup hook fired is depending on its host for correctness. A cold
        # /studies/enums must not stop the process from booting either (T02); /health says so,
        # and `VocabularyCache.get` loads on first use regardless.
        app.state.vocabulary_ready = await vocabulary_cache.warm(CTGClient(owned))
        try:
            yield
        finally:
            if transport is None:
                await owned.aclose()

    app = FastAPI(
        title="Cheiron",
        version="0.1.0",
        summary="Natural-language questions about clinical trials in; cited visualization "
        "specifications out.",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.transport = owned
    app.state.vocabulary_cache = vocabulary_cache
    app.state.plan_cache = TTLStore[CachedPlan]()
    app.state.result_cache = TTLStore[AnalyzeResponse](ttl=RESULT_TTL_SECONDS)
    app.state.vocabulary_ready = False

    install_request_id_middleware(app)
    install_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "llm_enabled": resolved.llm_enabled,
            # Either the startup warm succeeded or a request has since loaded it lazily. Reading
            # only the warm flag reported "unavailable" forever on a host that skips lifespan,
            # while /analyze worked fine.
            "vocabulary": "ok"
            if (app.state.vocabulary_ready or vocabulary_cache.loaded)
            else "unavailable",
            # Caching is a stated property (SPEC §1, §7), so it is observable rather than assumed.
            "cache": {
                "plan": app.state.plan_cache.stats(),
                "result": app.state.result_cache.stats(),
            },
        }

    demo_page = Path(__file__).resolve().parent.parent / "demo" / "index.html"
    demo_assets = demo_page.parent / "assets"
    if demo_assets.is_dir():
        # The mark and wordmark, served as files rather than inlined: a favicon has to be
        # reachable from /docs too, and base64 in the page would not be.
        app.mount("/assets", StaticFiles(directory=demo_assets), name="assets")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def demo() -> HTMLResponse:
        """A renderer built only from `encoding` + `data`, as a frontend engineer would have to.

        It is a demo, but it is also a test of the output contract: every chart type is drawn by
        reading the specification, never by knowing which question was asked. Anything the demo
        cannot draw without special-casing is a gap in the spec, not in the demo.
        """
        if not demo_page.exists():  # pragma: no cover - only if the file is not shipped
            return HTMLResponse("<p>demo/index.html is missing</p>", status_code=404)
        return HTMLResponse(demo_page.read_text(encoding="utf-8"))

    @app.post("/analyze", response_model=AnalyzeResponse)
    async def analyze_endpoint(body: AnalyzeRequest, request: Request) -> AnalyzeResponse:
        return await analyze(
            body,
            transport=request.app.state.transport,
            vocabulary_cache=request.app.state.vocabulary_cache,
            settings=request.app.state.settings,
            plan_cache=request.app.state.plan_cache,
            result_cache=request.app.state.result_cache,
            completer=resolved_completer,
        )

    return app
