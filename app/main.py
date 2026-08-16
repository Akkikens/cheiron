from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.ctg.client import CTGClient, CTGTransport
from app.ctg.vocab import VocabularyCache
from app.errors import install_error_handlers, install_request_id_middleware


def create_app(
    settings: Settings | None = None, *, transport: CTGTransport | None = None
) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = transport or CTGTransport(resolved)
        app.state.transport = owned
        app.state.vocabulary_cache = VocabularyCache()
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

    install_request_id_middleware(app)
    install_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "llm_enabled": resolved.llm_enabled,
            "vocabulary": "ok" if app.state.vocabulary_ready else "unavailable",
        }

    return app
