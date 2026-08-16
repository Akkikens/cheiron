"""Every failure leaves the service as SPEC §4.5's envelope, with SPEC §4.5's status."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from app.errors import (
    INTERNAL_ERROR_MESSAGE,
    REQUEST_ID_HEADER,
    STATUS_BY_CODE,
    CheironError,
    ErrorCode,
    install_error_handlers,
    install_request_id_middleware,
)

# Transcribed from SPEC §4.5's table, deliberately by hand rather than imported, so that
# editing STATUS_BY_CODE without editing the spec fails here.
SPEC_STATUS_TABLE = {
    "invalid_request": 422,
    "unplannable_query": 422,
    "upstream_error": 502,
    "upstream_timeout": 504,
    "upstream_circuit_open": 503,
    "rate_limited": 429,
    "internal_error": 500,
}

LEAKY_SECRET = "sk-live-do-not-leak-this"


class _StubAnalyzeRequest(BaseModel):
    """Stands in for T04's `AnalyzeRequest`; only the §2.1 rules this test needs."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=1000)
    start_year: int | None = Field(default=None, ge=1900, le=2100)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_request_id_middleware(app)
    install_error_handlers(app)

    @app.get("/boom/{code}")
    async def boom(code: ErrorCode) -> None:
        raise CheironError(code, f"deliberate {code}")

    @app.get("/boom-with-retry")
    async def boom_with_retry() -> None:
        raise CheironError(
            ErrorCode.UPSTREAM_CIRCUIT_OPEN,
            "Breaker open for clinicaltrials.gov.",
            retry_after_seconds=5,
        )

    @app.get("/boom-with-details")
    async def boom_with_details() -> None:
        raise CheironError(
            ErrorCode.UNPLANNABLE_QUERY,
            "Trial metadata cannot answer this.",
            details=[{"suggestion": "Ask about phases, status, sponsors, or start year."}],
        )

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError(f"boom while talking to {LEAKY_SECRET}")

    @app.post("/stub-analyze")
    async def stub_analyze(body: _StubAnalyzeRequest) -> dict[str, str]:
        return {"query": body.query}

    return TestClient(app, raise_server_exceptions=False)


def test_status_table_matches_spec() -> None:
    assert {str(code): status for code, status in STATUS_BY_CODE.items()} == SPEC_STATUS_TABLE


def test_every_code_has_a_status() -> None:
    assert set(ErrorCode) == set(STATUS_BY_CODE)


@pytest.mark.parametrize("code", list(ErrorCode))
def test_error_maps_to_documented_status_and_envelope(client: TestClient, code: ErrorCode) -> None:
    response = client.get(f"/boom/{code}")

    assert response.status_code == SPEC_STATUS_TABLE[str(code)]

    body = response.json()
    assert set(body) == {"error"}
    error = body["error"]
    assert error["code"] == str(code)
    assert error["message"] == f"deliberate {code}"
    assert len(error["request_id"]) == 32
    # Optional members stay absent rather than arriving as nulls.
    assert set(error) == {"code", "message", "request_id"}


def test_request_id_is_echoed_in_the_header(client: TestClient) -> None:
    response = client.get(f"/boom/{ErrorCode.UPSTREAM_ERROR}")
    assert response.headers[REQUEST_ID_HEADER] == response.json()["error"]["request_id"]


def test_request_id_is_unique_per_request(client: TestClient) -> None:
    seen = {client.get("/boom/rate_limited").json()["error"]["request_id"] for _ in range(5)}
    assert len(seen) == 5


def test_retry_after_appears_in_body_and_header(client: TestClient) -> None:
    response = client.get("/boom-with-retry")

    assert response.status_code == 503
    assert response.json()["error"]["retry_after_seconds"] == 5
    assert response.headers["Retry-After"] == "5"


def test_details_are_surfaced_when_present(client: TestClient) -> None:
    response = client.get("/boom-with-details")

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unplannable_query"
    assert error["details"] == [
        {"suggestion": "Ask about phases, status, sponsors, or start year."}
    ]


def test_missing_required_field_is_422_invalid_request(client: TestClient) -> None:
    response = client.post("/stub-analyze", json={})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"], "invalid_request must say which field failed"
    assert error["details"][0]["field"] == "query"


def test_out_of_range_field_names_itself_in_details(client: TestClient) -> None:
    response = client.post("/stub-analyze", json={"query": "trials by phase", "start_year": 1600})

    assert response.status_code == 422
    fields = [detail["field"] for detail in response.json()["error"]["details"]]
    assert fields == ["start_year"]


def test_unknown_field_is_rejected_not_ignored(client: TestClient) -> None:
    """SPEC §2.1: a typo'd filter that silently does nothing is worse than an error."""
    response = client.post("/stub-analyze", json={"query": "trials by phase", "phaze": ["PHASE2"]})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert [detail["field"] for detail in error["details"]] == ["phaze"]


def test_unhandled_exception_is_500_internal_error(client: TestClient) -> None:
    response = client.get("/crash")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["message"] == INTERNAL_ERROR_MESSAGE
    assert error["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_our_bugs_are_never_attributed_to_upstream(client: TestClient) -> None:
    """SPEC §4.5: a server-side fault is `internal_error`, not `upstream_error`."""
    assert client.get("/crash").json()["error"]["code"] != "upstream_error"


def test_500_leaks_neither_exception_text_nor_traceback(client: TestClient) -> None:
    body = client.get("/crash").text

    assert LEAKY_SECRET not in body
    assert "RuntimeError" not in body
    assert "Traceback" not in body


def test_crash_is_logged_with_its_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The trace has to reach the operator, or the request_id buys the caller nothing."""
    with caplog.at_level("ERROR", logger="cheiron.errors"):
        response = client.get("/crash")

    request_id = response.json()["error"]["request_id"]
    assert request_id in caplog.text
    assert LEAKY_SECRET in caplog.text


def test_unknown_route_still_returns_the_envelope(client: TestClient) -> None:
    response = client.get("/no-such-route")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["request_id"]
