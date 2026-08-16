from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.errors import install_error_handlers, install_request_id_middleware


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()

    app = FastAPI(
        title="Cheiron",
        version="0.1.0",
        summary="Natural-language questions about clinical trials in; cited visualization "
        "specifications out.",
    )

    install_request_id_middleware(app)
    install_error_handlers(app)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "llm_enabled": resolved.llm_enabled}

    return app
