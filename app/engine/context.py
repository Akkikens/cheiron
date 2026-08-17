"""Per-request execution context and the two exceptions that stop a run.

Both budgets are spent, not merely observed. A deadline that is only checked at the end is a
deadline that has already been missed, so `spend` is called *before* each wave rather than
after it, and it names what the allowance went on: "budget exhausted" alone tells an operator
nothing they can act on.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.errors import CheironError, ErrorCode
from app.models.request import Options


class DataTimestampChanged(Exception):  # noqa: N818  (name frozen by BUILD-PLAN §4)
    """Upstream published a new dataset mid-run; the numbers would mix two revisions.

    Carries both timestamps because "the data changed" is not actionable and "2026-08-14 became
    2026-08-15 between the preflight and the fan-out" is.
    """

    def __init__(self, captured: str, observed: str) -> None:
        super().__init__(
            f"ClinicalTrials.gov data timestamp changed from {captured} to {observed} during "
            "the run; counts from two revisions cannot be combined."
        )
        self.captured = captured
        self.observed = observed


class BudgetExhausted(Exception):  # noqa: N818  (name frozen by BUILD-PLAN §4)
    """The upstream request allowance ran out. Surfaced as 504, never as a partial answer."""


@dataclass
class RunContext:
    client: CTGClient
    vocab: Vocabulary
    options: Options
    deadline: float
    upstream_budget: int
    data_timestamp: str
    budget_ms: int
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    clock: Callable[[], float] = time.monotonic
    _spent: int = 0

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining_ms(self) -> int:
        return max(0, int((self.deadline - self.clock()) * 1000))

    def spend(self, n: int, purpose: str) -> None:
        """Reserve `n` upstream requests for `purpose`, or refuse the whole wave.

        Refuses up front rather than mid-wave: discovering the budget is gone after 12 of 20
        buckets have landed leaves you holding a partial aggregation, which SPEC §4.5 forbids
        rendering anyway.
        """
        if self.remaining_ms <= 0:
            raise BudgetExhausted(
                f"The {self.budget_ms}ms request budget elapsed before {purpose}; "
                f"{self._spent} upstream requests had been issued."
            )

        if self._spent + n > self.upstream_budget:
            raise BudgetExhausted(
                f"{purpose} needs {n} upstream requests but only "
                f"{self.upstream_budget - self._spent} of {self.upstream_budget} remain "
                f"({self._spent} already spent)."
            )
        self._spent += n

    def reset_spend(self, to: int = 0) -> None:
        """Restore the spend ledger. Used by SPEC §7's whole-group-by retry after a timestamp move.

        Without this, the failed attempt's spend stays charged and the retry almost always
        raises BudgetExhausted before it can redo the fan-out.
        """
        if to < 0 or to > self._spent:
            raise ValueError(f"reset_spend({to}) is outside 0..{self._spent}")
        self._spent = to

    async def observed_data_timestamp(self) -> str:
        """A live `/version` read, not `self.data_timestamp`.

        `CTGClient.version()` revalidates with `If-None-Match` on every call, so this costs one
        conditional request and returns a fresh value whenever the dataset actually moved.
        Reading the captured field here instead would make SPEC §7's retry guarantee theatre:
        the check could never fail.
        """
        version = await self.client.version()
        return version.data_timestamp

    async def assert_data_unchanged(self) -> None:
        observed = await self.observed_data_timestamp()
        if observed != self.data_timestamp:
            raise DataTimestampChanged(self.data_timestamp, observed)


def new_context(
    client: CTGClient,
    vocab: Vocabulary,
    options: Options,
    *,
    settings: Settings,
    data_timestamp: str,
    clock: Callable[[], float] = time.monotonic,
) -> RunContext:
    """Build a context from settings, so the deadline and the budget derive from one source."""
    return RunContext(
        client=client,
        vocab=vocab,
        options=options,
        deadline=clock() + settings.request_budget_ms / 1000,
        upstream_budget=settings.max_upstream_requests,
        data_timestamp=data_timestamp,
        budget_ms=settings.request_budget_ms,
        clock=clock,
    )


def budget_error(exc: BudgetExhausted) -> CheironError:
    """SPEC §4.5: a budget failure is a timeout, and it says what it spent."""
    return CheironError(ErrorCode.UPSTREAM_TIMEOUT, str(exc))
