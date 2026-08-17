"""Error taxonomy and the single JSON envelope every failure is rendered into (SPEC §4.5).

Upstream ClinicalTrials.gov failures arrive as `text/plain` (see `docs/CTG-API-NOTES.md` §4);
nothing in this module inspects a body, it only guarantees that whatever went wrong leaves
here as `{"error": {code, message, request_id, retry_after_seconds?, details?}}`.
"""

from __future__ import annotations

import html
import logging
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("cheiron.errors")

REQUEST_ID_HEADER = "X-Request-Id"

INTERNAL_ERROR_MESSAGE = "Internal error. Quote the request_id when reporting this."
"""The only message a 500 ever carries: no exception text, no traceback, no attribution."""


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNPLANNABLE_QUERY = "unplannable_query"
    UPSTREAM_ERROR = "upstream_error"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    UPSTREAM_CIRCUIT_OPEN = "upstream_circuit_open"
    RATE_LIMITED = "rate_limited"
    INTERNAL_ERROR = "internal_error"


STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 422,
    ErrorCode.UNPLANNABLE_QUERY: 422,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.UPSTREAM_CIRCUIT_OPEN: 503,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.INTERNAL_ERROR: 500,
}


class CheironError(Exception):
    """A failure the caller is allowed to see, carrying its documented status."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: list[dict[str, Any]] | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: list[dict[str, Any]] = details or []
        self.retry_after_seconds = retry_after_seconds
        self.status = STATUS_BY_CODE[code]


def request_id_of(request: Request) -> str:
    """The per-request uuid4 hex, minted here if the middleware never ran."""
    existing: str | None = getattr(request.state, "request_id", None)
    if existing is None:
        existing = uuid.uuid4().hex
        request.state.request_id = existing
    return existing


def error_response(
    code: ErrorCode,
    message: str,
    *,
    status: int,
    request_id: str,
    retry_after_seconds: int | None = None,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"code": str(code), "message": message, "request_id": request_id}
    if retry_after_seconds is not None:
        body["retry_after_seconds"] = retry_after_seconds
    if details:
        body["details"] = details

    headers = {REQUEST_ID_HEADER: request_id}
    if retry_after_seconds is not None:
        headers["Retry-After"] = str(retry_after_seconds)

    return JSONResponse({"error": body}, status_code=status, headers=headers)


def _validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for err in exc.errors():
        # Drop the leading "body" segment so `field` names the caller's own field.
        location = [str(part) for part in err.get("loc", ()) if part != "body"]
        details.append(
            {
                "field": ".".join(location) or "(body)",
                "message": err.get("msg", "invalid value"),
                "type": err.get("type", "value_error"),
            }
        )
    return details


async def _handle_cheiron_error(request: Request, exc: CheironError) -> Response:
    return error_response(
        exc.code,
        exc.message,
        status=exc.status,
        request_id=request_id_of(request),
        retry_after_seconds=exc.retry_after_seconds,
        details=exc.details,
    )


async def _handle_validation_error(request: Request, exc: RequestValidationError) -> Response:
    return error_response(
        ErrorCode.INVALID_REQUEST,
        "Request failed validation.",
        status=STATUS_BY_CODE[ErrorCode.INVALID_REQUEST],
        request_id=request_id_of(request),
        details=_validation_details(exc),
    )


async def _handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    # Routing-level failures (404, 405) still owe the caller the documented envelope.
    request_id = request_id_of(request)
    if exc.status_code == 404 and _wants_html(request):
        page = _not_found_page(str(exc.detail), request_id)
        if page is not None:
            return HTMLResponse(page, status_code=404, headers={"X-Request-Id": request_id})
    return error_response(
        ErrorCode.INVALID_REQUEST,
        str(exc.detail),
        status=exc.status_code,
        request_id=request_id,
    )


def _wants_html(request: Request) -> bool:
    """A browser gets a page; anything else keeps the JSON envelope, byte for byte.

    Content negotiation rather than a blanket switch: `curl`, the tests and any API client send
    `*/*` or `application/json` and must not start receiving markup because a human wandered
    onto a bad URL.
    """
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _not_found_page(detail: str, request_id: str) -> str | None:
    """The 404 shows the JSON an API client would have received, with this request's id.

    A 404 is where a reviewer first meets the error contract, so it demonstrates it rather than
    apologising. Returns None when the file is absent, and the JSON envelope answers instead.
    """
    page = Path(__file__).resolve().parent.parent / "demo" / "404.html"
    if not page.is_file():  # pragma: no cover - only when demo/ is not shipped
        return None
    return (
        page.read_text(encoding="utf-8")
        .replace("__MESSAGE__", html.escape(detail, quote=True))
        .replace("__REQUEST_ID__", html.escape(request_id, quote=True))
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    request_id = request_id_of(request)
    # The trace goes to the operator; the caller gets a request_id to quote and nothing else.
    logger.exception("unhandled error [request_id=%s]", request_id, exc_info=exc)
    return error_response(
        ErrorCode.INTERNAL_ERROR,
        INTERNAL_ERROR_MESSAGE,
        status=STATUS_BY_CODE[ErrorCode.INTERNAL_ERROR],
        request_id=request_id,
    )


_Handler = Callable[[Request, Exception], Awaitable[Response]]


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(CheironError, cast(_Handler, _handle_cheiron_error))
    app.add_exception_handler(RequestValidationError, cast(_Handler, _handle_validation_error))
    app.add_exception_handler(StarletteHTTPException, cast(_Handler, _handle_http_exception))
    app.add_exception_handler(Exception, _handle_unexpected_error)


def install_request_id_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def _assign_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request_id_of(request)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
