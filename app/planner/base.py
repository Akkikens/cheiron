"""The planner seam. SPEC §5.5.

Both planners return the same thing, so the pipeline never branches on which one ran — the
only observable difference is `PlanResult.planner`, which lands in `meta.planner`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.ctg.vocab import Vocabulary
from app.models.plan import AnalysisPlan
from app.models.request import AnalyzeRequest
from app.models.response import PlannerName


@dataclass(frozen=True)
class PlanResult:
    plan: AnalysisPlan
    planner: PlannerName
    attempts: int


class Planner(Protocol):
    async def plan(self, req: AnalyzeRequest, vocab: Vocabulary) -> PlanResult: ...
