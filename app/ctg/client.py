"""The only code that talks to ClinicalTrials.gov.

Implements SPEC §7's upstream courtesy and every transport gotcha in `CTG-API-NOTES.md` §4.
The governing asymmetry, measured in T01: upstream fails **loudly** on a malformed argument
(400, `text/plain`) and **silently** on a missing scope (HTTP 200, wrong number). You cannot
tell which category a parameter is in without testing it, so nothing here defaults a missing
field to a benign value: an absent `totalCount` is an error, not a zero.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final, Self, TypeVar

import httpx

from app.config import Settings
from app.errors import CheironError, ErrorCode

logger = logging.getLogger("cheiron.ctg")

T = TypeVar("T")

MAX_PAGE_SIZE: Final = 1000
"""Upstream clamps above this silently (notes §3), so we refuse before issuing."""

COUNT_PARAMS: Final[Mapping[str, str]] = {
    "countTotal": "true",
    "pageSize": "1",
    "fields": "NCTId",
}

PAGING_PARAMS: Final = frozenset({"countTotal", "pageSize", "pageToken"})
"""The only params a `pageToken` continuation may legally differ by (notes §3)."""

USER_AGENT: Final = "cheiron/0.1 (+https://github.com/akshay/cheiron)"

DEFAULT_RATE_PER_SECOND: Final = 8.0
DEFAULT_BURST: Final = 16
DEFAULT_ATTEMPTS: Final = 3
DEFAULT_BACKOFF_BASE_S: Final = 0.2
BREAKER_THRESHOLD: Final = 5
BREAKER_RESET_S: Final = 30.0

_ERROR_BODY_LIMIT: Final = 400


@dataclass(frozen=True)
class Version:
    api_version: str
    data_timestamp: str


@dataclass(frozen=True)
class StudyPage:
    studies: list[dict[str, Any]]
    next_page_token: str | None
    total_count: int | None


class TokenBucket:
    """Self-imposed rate limit: upstream publishes none, so we invent a polite one."""

    def __init__(
        self,
        rate_per_second: float = DEFAULT_RATE_PER_SECOND,
        burst: int = DEFAULT_BURST,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._rate = rate_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._updated = clock()
        self._clock = clock
        self._sleep = sleep

    async def acquire(self) -> None:
        while True:
            now = self._clock()
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await self._sleep((1.0 - self._tokens) / self._rate)


class CircuitBreaker:
    """Closed → (5 consecutive failures) → open 30 s → single half-open probe."""

    def __init__(
        self,
        *,
        threshold: int = BREAKER_THRESHOLD,
        reset_after_s: float = BREAKER_RESET_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._reset_after_s = reset_after_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def check(self) -> None:
        """Raise if the breaker forbids this call. No awaits, so it is atomic."""
        if self._opened_at is None:
            return

        elapsed = self._clock() - self._opened_at
        remaining = self._reset_after_s - elapsed
        if remaining > 0 or self._probing:
            raise CheironError(
                ErrorCode.UPSTREAM_CIRCUIT_OPEN,
                "ClinicalTrials.gov is failing; requests are paused to avoid making it worse.",
                retry_after_seconds=max(1, int(remaining) + 1),
            )
        self._probing = True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        if self._probing:
            # The half-open probe failed: straight back to open for another full window.
            self._probing = False
            self._opened_at = self._clock()
            return

        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock()
            logger.warning("circuit breaker opened after %d consecutive failures", self._failures)


class CTGTransport:
    """App-scoped: the one `httpx.AsyncClient` plus the courtesy machinery it shares.

    One instance per process. `CTGClient` is the per-request facade over it, because
    `query_log` and `pageToken` bindings belong to a single analysis, not to the process.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        rng: random.Random | None = None,
        attempts: int = DEFAULT_ATTEMPTS,
    ) -> None:
        self._settings = settings
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=settings.ctg_base_url,
            http2=True,
            headers={
                "Accept-Encoding": "gzip",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=8.0, pool=3.0),
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)
        self._bucket = TokenBucket(clock=clock, sleep=sleep)
        self._breaker = CircuitBreaker(clock=clock)
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._attempts = attempts
        # How long a `Retry-After` may actually be slept for, per attempt. Divided by the
        # attempt count rather than halved: a `get()` sleeps between every attempt, so "half the
        # budget" still added up to the whole budget across two retries: the same mistake as
        # the flat 30s ceiling it replaced, one step smaller. No floor, either: flooring at 1s
        # exceeded the budget outright when REQUEST_BUDGET_MS was configured below 2000.
        #
        # The value *reported* to the caller stays upstream's own, uncapped: clamping that would
        # have a well-behaved client retry too early and be rate limited again.
        self._max_retry_sleep_s = settings.request_budget_ms / 1000 / max(attempts, 1)
        self._revalidated: dict[str, tuple[str, Any]] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def get(
        self,
        path: str,
        params: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
        query_log: list[str] | None = None,
    ) -> httpx.Response:
        last_error: CheironError | None = None

        for attempt in range(1, self._attempts + 1):
            self._breaker.check()
            await self._bucket.acquire()

            # Reset per attempt: on a transport failure there is no response to read a
            # Retry-After from, and carrying the previous attempt's response would be worse
            # than having none.
            retry_after: int | None = None

            try:
                async with self._semaphore:
                    response = await self._http.get(
                        path, params=params, headers=dict(headers or {})
                    )
            except httpx.TimeoutException as exc:
                self._log(query_log, exc.request)
                self._breaker.record_failure()
                last_error = CheironError(
                    ErrorCode.UPSTREAM_TIMEOUT,
                    f"ClinicalTrials.gov did not respond within the timeout for {path}.",
                )
            except httpx.HTTPError as exc:
                self._log(query_log, getattr(exc, "request", None))
                self._breaker.record_failure()
                last_error = CheironError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"Could not reach ClinicalTrials.gov: {type(exc).__name__}.",
                )
            else:
                self._log(query_log, response.request)

                if response.status_code < 400:
                    self._breaker.record_success()
                    return response

                if response.status_code < 500 and response.status_code != 429:
                    # A 4xx is a healthy server rejecting *our* expression. Deterministic:
                    # never retried, and it does not count against the breaker.
                    self._breaker.record_success()
                    raise self._client_error(response, params)

                self._breaker.record_failure()
                if response.status_code == 429:
                    retry_after = _retry_after_seconds(response)
                    last_error = self._rate_limited(response)
                else:
                    last_error = self._server_error(response)

            if attempt < self._attempts:
                # A 429 is upstream telling us the rate, so honour Retry-After rather than
                # racing back in under the jittered backoff. This module exists to be a good
                # citizen of a public NIH service; ignoring the one explicit signal it sends
                # would be the least courteous thing in it.
                await self._sleep(
                    min(retry_after, self._max_retry_sleep_s)
                    if retry_after is not None
                    else self._backoff(attempt)
                )

        assert last_error is not None
        raise last_error

    def _backoff(self, attempt: int) -> float:
        """Exponential with full jitter, so a fleet of workers does not resynchronise."""
        return self._rng.uniform(0.0, DEFAULT_BACKOFF_BASE_S * (2 ** (attempt - 1)))

    def _log(self, query_log: list[str] | None, request: httpx.Request | None) -> None:
        if query_log is not None and request is not None:
            query_log.append(str(request.url))

    @staticmethod
    def _body_snippet(response: httpx.Response) -> str:
        """Errors are `text/plain` (notes §4). `.json()` is never called on a failure."""
        return response.text.strip()[:_ERROR_BODY_LIMIT]

    def _client_error(self, response: httpx.Response, params: Mapping[str, str]) -> CheironError:
        predicate = params.get("filter.advanced")
        body = self._body_snippet(response)
        message = f"ClinicalTrials.gov rejected our request with {response.status_code}: {body}"
        if predicate is not None:
            message += f" (filter.advanced={predicate})"
        return CheironError(
            ErrorCode.UPSTREAM_ERROR,
            message,
            details=[
                {
                    "upstream_status": response.status_code,
                    "upstream_body": body,
                    "filter.advanced": predicate,
                }
            ],
        )

    def _rate_limited(self, response: httpx.Response) -> CheironError:
        """SPEC §4.5's `rate_limited`, carrying upstream's own `Retry-After` when it sends one."""
        retry_after = _retry_after_seconds(response)
        return CheironError(
            ErrorCode.RATE_LIMITED,
            "ClinicalTrials.gov is rate limiting this client"
            + (f"; retry after {retry_after}s." if retry_after is not None else "."),
            retry_after_seconds=retry_after,
            details=[{"upstream_status": 429}],
        )

    def _server_error(self, response: httpx.Response) -> CheironError:
        return CheironError(
            ErrorCode.UPSTREAM_ERROR,
            f"ClinicalTrials.gov returned {response.status_code}: {self._body_snippet(response)}",
            details=[{"upstream_status": response.status_code}],
        )

    async def get_revalidated(
        self,
        path: str,
        parse: Callable[[httpx.Response], T],
        *,
        query_log: list[str] | None = None,
    ) -> T:
        """`If-None-Match` revalidation: a 304 returns the cached object, unparsed.

        The tag is dataset-scoped rather than per-query (notes §4/§7), so one entry per
        path is enough and `/version` and `/studies/enums` share a revision.
        """
        cached = self._revalidated.get(path)
        headers = {"If-None-Match": cached[0]} if cached is not None else {}

        response = await self.get(path, {}, headers=headers, query_log=query_log)

        if response.status_code == 304 and cached is not None:
            return cached[1]  # type: ignore[no-any-return]

        value = parse(response)
        etag = response.headers.get("ETag")
        if etag is not None:
            self._revalidated[path] = (etag, value)
        return value


class CTGClient:
    """Per-request facade: owns this analysis's query log and `pageToken` bindings."""

    def __init__(self, transport: CTGTransport) -> None:
        self._transport = transport
        self._query_log: list[str] = []
        self._token_bindings: dict[str, str] = {}

    @property
    def query_log(self) -> list[str]:
        return list(self._query_log)

    async def count(self, params: Mapping[str, str]) -> int:
        response = await self._transport.get(
            "/studies", {**params, **COUNT_PARAMS}, query_log=self._query_log
        )
        payload = response.json()
        total = payload.get("totalCount")
        if total is None:
            # Silent-wrong is upstream's worst failure mode; refuse to invent a zero.
            raise CheironError(
                ErrorCode.UPSTREAM_ERROR,
                "ClinicalTrials.gov returned no totalCount for a countTotal=true request.",
                details=[{"params": dict(params)}],
            )
        return int(total)

    async def page(self, params: Mapping[str, str], page_token: str | None = None) -> StudyPage:
        self._reject_oversized_page(params)
        binding = self._binding(params)

        request_params = dict(params)
        if page_token is not None:
            self._check_token(page_token, binding)
            request_params["pageToken"] = page_token

        response = await self._transport.get("/studies", request_params, query_log=self._query_log)
        payload = response.json()

        next_token = payload.get("nextPageToken")
        if next_token is not None:
            self._token_bindings[next_token] = binding

        return StudyPage(
            studies=payload.get("studies", []),
            next_page_token=next_token,
            total_count=payload.get("totalCount"),
        )

    async def version(self) -> Version:
        return await self._transport.get_revalidated(
            "/version", _parse_version, query_log=self._query_log
        )

    async def enums(self) -> dict[str, list[str]]:
        return await self._transport.get_revalidated(
            "/studies/enums", _parse_enums, query_log=self._query_log
        )

    @staticmethod
    def _reject_oversized_page(params: Mapping[str, str]) -> None:
        raw = params.get("pageSize")
        if raw is None:
            return
        if int(raw) > MAX_PAGE_SIZE:
            raise ValueError(
                f"pageSize={raw} exceeds the upstream maximum of {MAX_PAGE_SIZE}; "
                "upstream clamps silently and would return 1000 with HTTP 200."
            )

    @staticmethod
    def _binding(params: Mapping[str, str]) -> str:
        material = "&".join(
            f"{key}={params[key]}" for key in sorted(params) if key not in PAGING_PARAMS
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _check_token(self, page_token: str, binding: str) -> None:
        issued_for = self._token_bindings.get(page_token)
        if issued_for is None:
            raise ValueError(
                "pageToken was not issued by this client; upstream accepts foreign tokens "
                "and silently returns the wrong query's results at the old offset."
            )
        if issued_for != binding:
            raise ValueError(
                "pageToken was issued for a different parameter set; continuing would "
                "silently page through a different query (notes §3)."
            )


def _parse_version(response: httpx.Response) -> Version:
    payload = response.json()
    return Version(
        api_version=payload["apiVersion"],
        data_timestamp=payload["dataTimestamp"],
    )


def _parse_enums(response: httpx.Response) -> dict[str, list[str]]:
    """`/studies/enums` is a JSON array of `{type, values:[{value, legacyValue}], pieces}`."""
    payload = response.json()
    return {entry["type"]: [value["value"] for value in entry["values"]] for entry in payload}


def _retry_after_seconds(response: httpx.Response) -> int | None:
    """`Retry-After` as whole seconds. Header-date form is not parsed; absent beats guessed."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    # `inf` and `nan` parse as floats and then blow up in int(); a malformed header must not
    # become a 500 in a module whose whole job is surviving upstream weirdness.
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return None
    return max(0, int(seconds))
