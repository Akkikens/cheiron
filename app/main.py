from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from app.analyze import analyze
from app.cache import RESULT_TTL_SECONDS, TTLStore
from app.config import Settings, get_settings
from app.ctg.client import CTGClient, CTGTransport
from app.ctg.vocab import VocabularyCache
from app.errors import install_error_handlers, install_request_id_middleware
from app.models.plan import AnalysisPlan
from app.models.request import AnalyzeRequest
from app.models.response import AnalyzeResponse
from app.planner.llm import ChatCompleter, openai_completer


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = transport or CTGTransport(resolved)
        app.state.transport = owned
        app.state.vocabulary_cache = VocabularyCache()
        app.state.plan_cache = TTLStore[AnalysisPlan]()
        app.state.result_cache = TTLStore[AnalyzeResponse](ttl=RESULT_TTL_SECONDS)
        # A cold /studies/enums must not stop the process from booting (T02); /health says so.
        app.state.vocabulary_ready = await app.state.vocabulary_cache.warm(CTGClient(owned))
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

    install_request_id_middleware(app)
    install_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "llm_enabled": resolved.llm_enabled,
            "vocabulary": "ok" if app.state.vocabulary_ready else "unavailable",
            # Caching is a stated property (SPEC §1, §7), so it is observable rather than assumed.
            "cache": {
                "plan": app.state.plan_cache.stats(),
                "result": app.state.result_cache.stats(),
            },
        }

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
