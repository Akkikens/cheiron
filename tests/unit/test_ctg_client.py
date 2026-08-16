"""Transport behaviour, all of it offline. See `docs/CTG-API-NOTES.md` sections 3 and 4."""

from __future__ import annotations

import asyncio
import json
import random

import httpx
import pytest

from app.config import Settings
from app.ctg.client import (
    MAX_PAGE_SIZE,
    CircuitBreaker,
    CTGClient,
    CTGTransport,
    TokenBucket,
)
from app.errors import CheironError, ErrorCode
from tests.conftest import Handler, stub_transport


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _studies(total: int = 7, next_token: str | None = None) -> str:
    body: dict[str, object] = {"totalCount": total, "studies": []}
    if next_token is not None:
        body["nextPageToken"] = next_token
    return json.dumps(body)


def _client(settings: Settings, handler: Handler, **kwargs: object) -> CTGClient:
    return CTGClient(stub_transport(settings, handler, **kwargs))


# --- text/plain error dispatch (notes §4) ------------------------------------------------


async def test_malformed_predicate_becomes_502_naming_the_predicate(settings: Settings) -> None:
    predicate = "AREA[Nope]PHASE2"

    def handler(_request: httpx.Request) -> httpx.Response:
        # Deliberately not valid JSON: .json() on this body would raise.
        return httpx.Response(
            400,
            text="Error parsing query in advanced filter: Unknown area name: 'Nope'",
            headers={"content-type": "text/plain"},
        )

    client = _client(settings, handler)

    with pytest.raises(CheironError) as caught:
        await client.count({"filter.advanced": predicate})

    error = caught.value
    assert error.code is ErrorCode.UPSTREAM_ERROR
    assert error.status == 502
    assert predicate in error.message
    assert "Unknown area name" in error.message
    assert error.details[0]["upstream_status"] == 400


async def test_a_4xx_is_never_retried(settings: Settings) -> None:
    """A bad predicate is deterministic; retrying it just doubles the load."""
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="`bogusParam` is unknown parameter")

    client = _client(settings, handler)

    with pytest.raises(CheironError):
        await client.count({"bogusParam": "1"})

    assert attempts == 1


async def test_5xx_is_retried_then_surfaces_as_502(settings: Settings) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _client(settings, handler, rng=random.Random(0))

    with pytest.raises(CheironError) as caught:
        await client.count({"query.cond": "cancer"})

    assert attempts == 3
    assert caught.value.code is ErrorCode.UPSTREAM_ERROR


async def test_timeout_becomes_504(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    client = _client(settings, handler, rng=random.Random(0))

    with pytest.raises(CheironError) as caught:
        await client.count({"query.cond": "cancer"})

    assert caught.value.code is ErrorCode.UPSTREAM_TIMEOUT
    assert caught.value.status == 504


async def test_a_transient_5xx_recovers_on_retry(settings: Settings) -> None:
    responses = [httpx.Response(503, text="down"), httpx.Response(200, text=_studies(42))]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = _client(settings, handler, rng=random.Random(0))

    assert await client.count({"query.cond": "cancer"}) == 42


# --- count() (notes §3) -------------------------------------------------------------------


async def test_count_sends_the_documented_count_params(settings: Settings) -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, text=_studies(1841))

    client = _client(settings, handler)
    total = await client.count({"filter.advanced": "AREA[Phase]PHASE2"})

    assert total == 1841
    params = seen[0].params
    assert params["countTotal"] == "true"
    assert params["pageSize"] == "1"
    assert params["fields"] == "NCTId"
    assert params["filter.advanced"] == "AREA[Phase]PHASE2"


async def test_missing_total_count_is_an_error_not_a_zero(settings: Settings) -> None:
    """The whole point of T01's asymmetry finding: never default a silent gap to 0."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=json.dumps({"studies": []}))

    client = _client(settings, handler)

    with pytest.raises(CheironError) as caught:
        await client.count({"query.cond": "cancer"})

    assert caught.value.code is ErrorCode.UPSTREAM_ERROR
    assert "totalCount" in caught.value.message


# --- pageSize and pageToken (notes §3) ----------------------------------------------------


async def test_oversized_page_is_rejected_before_any_request(settings: Settings) -> None:
    issued = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal issued
        issued += 1
        return httpx.Response(200, text=_studies())

    client = _client(settings, handler)

    with pytest.raises(ValueError, match="exceeds the upstream maximum"):
        await client.page({"query.cond": "cancer", "pageSize": "5000"})

    assert issued == 0


async def test_max_page_size_is_accepted(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_studies())

    client = _client(settings, handler)
    page = await client.page({"query.cond": "cancer", "pageSize": str(MAX_PAGE_SIZE)})

    assert page.next_page_token is None


async def test_token_replayed_against_different_params_raises(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_studies(next_token="TOKEN-A"))

    client = _client(settings, handler)
    first = await client.page({"query.cond": "cancer"})
    assert first.next_page_token == "TOKEN-A"

    with pytest.raises(ValueError, match="different parameter set"):
        await client.page({"query.cond": "diabetes"}, page_token="TOKEN-A")


async def test_token_is_accepted_for_the_params_that_minted_it(settings: Settings) -> None:
    tokens = ["TOKEN-A", None]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_studies(next_token=tokens.pop(0)))

    client = _client(settings, handler)
    first = await client.page({"query.cond": "cancer"})
    second = await client.page({"query.cond": "cancer"}, page_token=first.next_page_token)

    assert second.next_page_token is None


async def test_paging_params_do_not_change_the_binding(settings: Settings) -> None:
    """Notes §3: a continuation repeats everything except countTotal/pageSize/pageToken."""
    tokens = ["TOKEN-A", None]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_studies(next_token=tokens.pop(0)))

    client = _client(settings, handler)
    first = await client.page({"query.cond": "cancer", "countTotal": "true", "pageSize": "1000"})
    await client.page({"query.cond": "cancer"}, page_token=first.next_page_token)


async def test_a_foreign_token_is_refused(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_studies())

    client = _client(settings, handler)

    with pytest.raises(ValueError, match="not issued by this client"):
        await client.page({"query.cond": "cancer"}, page_token="TOKEN-FROM-SOMEWHERE-ELSE")


# --- ETag revalidation (notes §4) ---------------------------------------------------------


async def test_304_serves_the_cached_payload_without_reparsing(
    settings: Settings, enums_handler: Handler
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.headers.get("If-None-Match") == '"883b003/0.34.1/msspuzuw"':
            return httpx.Response(304, headers={"ETag": '"883b003/0.34.1/msspuzuw"'})
        return enums_handler(request)

    transport = stub_transport(settings, handler)
    first = await CTGClient(transport).enums()
    second = await CTGClient(transport).enums()

    assert calls == 2
    assert second is first, "a 304 must return the cached object, not re-parse the body"


async def test_version_parses_the_recorded_body(settings: Settings, enums_handler: Handler) -> None:
    client = _client(settings, enums_handler)
    version = await client.version()

    assert version.api_version == "2.0.5"
    assert version.data_timestamp == "2026-08-14T09:00:05"


async def test_enums_normalises_the_array_into_a_mapping(
    settings: Settings, enums_handler: Handler
) -> None:
    client = _client(settings, enums_handler)
    enums = await client.enums()

    assert len(enums) == 41
    assert enums["Phase"] == ["NA", "EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4"]
    assert "INTERVENTIONAL" in enums["StudyType"]


# --- courtesy: breaker, bucket, log -------------------------------------------------------


async def test_breaker_opens_after_five_failures_and_stops_calling(settings: Settings) -> None:
    clock = FakeClock()
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, text="down")

    transport = stub_transport(settings, handler, clock=clock, rng=random.Random(0), attempts=1)
    client = CTGClient(transport)

    for _ in range(5):
        with pytest.raises(CheironError):
            await client.count({"query.cond": "cancer"})
    assert attempts == 5

    with pytest.raises(CheironError) as caught:
        await client.count({"query.cond": "cancer"})

    assert caught.value.code is ErrorCode.UPSTREAM_CIRCUIT_OPEN
    assert caught.value.status == 503
    assert caught.value.retry_after_seconds is not None
    assert attempts == 5, "an open breaker must not issue an HTTP attempt"


async def test_breaker_allows_one_probe_after_the_window(settings: Settings) -> None:
    clock = FakeClock()
    outcome = [httpx.Response(500, text="down")] * 5 + [httpx.Response(200, text=_studies(3))]

    def handler(_request: httpx.Request) -> httpx.Response:
        return outcome.pop(0)

    transport = stub_transport(settings, handler, clock=clock, rng=random.Random(0), attempts=1)
    client = CTGClient(transport)

    for _ in range(5):
        with pytest.raises(CheironError):
            await client.count({"query.cond": "cancer"})

    clock.advance(31.0)
    assert await client.count({"query.cond": "cancer"}) == 3

    # A successful probe closes the breaker.
    outcome.append(httpx.Response(200, text=_studies(4)))
    assert await client.count({"query.cond": "cancer"}) == 4


async def test_a_4xx_does_not_trip_the_breaker(settings: Settings) -> None:
    """Our own bad predicate must not take out the client for everyone else."""
    clock = FakeClock()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Unknown area name: 'Nope'")

    transport = stub_transport(settings, handler, clock=clock, attempts=1)
    client = CTGClient(transport)

    for _ in range(6):
        with pytest.raises(CheironError) as caught:
            await client.count({"filter.advanced": "AREA[Nope]X"})
        assert caught.value.code is ErrorCode.UPSTREAM_ERROR


async def test_query_log_records_every_issued_url_in_order(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_studies(1))

    client = _client(settings, handler)
    await client.count({"filter.advanced": "AREA[Phase]PHASE2"})
    await client.count({"filter.advanced": "AREA[Phase]PHASE3"})

    log = client.query_log
    assert len(log) == 2
    assert "PHASE2" in log[0]
    assert "PHASE3" in log[1]
    assert all(url.startswith("https://clinicaltrials.gov/api/v2/studies") for url in log)


async def test_query_log_is_per_client_not_per_process(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_studies(1))

    transport = stub_transport(settings, handler)
    first, second = CTGClient(transport), CTGClient(transport)

    await first.count({"query.cond": "cancer"})

    assert len(first.query_log) == 1
    assert second.query_log == []


async def test_failed_requests_are_still_logged(settings: Settings) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="nope")

    client = _client(settings, handler)
    with pytest.raises(CheironError):
        await client.count({"query.cond": "cancer"})

    assert len(client.query_log) == 1


def test_query_log_cannot_be_mutated_by_a_caller(settings: Settings) -> None:
    client = CTGClient(stub_transport(settings, lambda r: httpx.Response(200, text=_studies())))
    client.query_log.append("https://example.invalid")

    assert client.query_log == []


async def test_concurrency_never_exceeds_the_configured_cap() -> None:
    settings = Settings(_env_file=None, llm_enabled=False, max_concurrency=3)
    in_flight = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            # Yield, so the other 11 requests get a chance to pile up behind the semaphore.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return httpx.Response(200, text=_studies(1))
        finally:
            in_flight -= 1

    http = httpx.AsyncClient(base_url=settings.ctg_base_url, transport=httpx.MockTransport(handler))
    transport = CTGTransport(settings, http=http)
    client = CTGClient(transport)

    await asyncio.gather(*(client.count({"query.cond": f"c{i}"}) for i in range(12)))

    assert peak <= 3


# --- courtesy primitives in isolation -----------------------------------------------------


async def test_token_bucket_waits_once_the_burst_is_spent() -> None:
    clock = FakeClock()
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds)

    bucket = TokenBucket(rate_per_second=2.0, burst=2, clock=clock, sleep=sleep)

    for _ in range(3):
        await bucket.acquire()

    assert waits == [0.5]


def test_breaker_reopens_when_the_probe_fails() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=2, reset_after_s=10.0, clock=clock)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open

    clock.advance(11.0)
    breaker.check()
    breaker.record_failure()

    clock.advance(1.0)
    with pytest.raises(CheironError):
        breaker.check()
