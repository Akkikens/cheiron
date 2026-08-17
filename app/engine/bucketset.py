"""`BucketSet` — the single handoff from the engine into render (BUILD-PLAN §4)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

from app.models.response import AggregationMode, Citation

Exactness = Literal["exact", "estimated"]


@dataclass(frozen=True)
class Bucket:
    key: str
    label: str
    value: float
    exactness: Exactness
    citations: list[Citation] = field(default_factory=list)
    citation_note: str | None = None


@dataclass(frozen=True)
class BucketSet:
    buckets: list[Bucket]
    total: int
    unclassified: int
    semantics: Literal["partition", "overlapping"]
    mode: AggregationMode
    """Narrower than BUILD-PLAN §4's `str`: the three modes are a closed set, and reusing the
    response Literal means an unknown mode fails here rather than at serialization."""
    aggregation_capped: bool = False
    """The aggregation stopped at `max_buckets`, so values beyond it were **never counted**.

    Distinct from narrowing, and the distinction is load-bearing: nothing here knows how many
    values were skipped, so no denominator can be quoted and the overlap cannot be computed —
    the memberships that would have made it up do not exist.
    """
    omitted_buckets: int = 0
    """Categories dropped when narrowing to what the chart plots. Counted, then not drawn.

    The opposite case to `aggregation_capped`: the total is known exactly, so coverage can say
    "3 of 10", and the overlap stays computable because `omitted_value` keeps their memberships.
    """
    omitted_value: float = 0.0
    """Value held by the narrowed-away categories.

    The overlap arithmetic describes the *result*, not the chart, so it needs the full
    membership count even after narrowing. Without this, `bucket_sum` fell to the plotted
    categories while `with_value` still covered every study and the reported overlap went
    negative — nonsense in the one block whose purpose is auditable arithmetic.
    """
    sample_size: int | None = None
    sample_coverage: float | None = None
    warnings: list[str] = field(default_factory=list)

    def plotted_only(self, kept: set[str]) -> BucketSet:
        """The same result narrowed to the categories the chart will actually draw."""
        keep = [bucket for bucket in self.buckets if bucket.key in kept]
        # Accumulates rather than replaces, and leaves `aggregation_capped` alone: narrowing a
        # set that was already capped upstream must not overwrite the record of either cut.
        return replace(
            self,
            buckets=keep,
            omitted_buckets=self.omitted_buckets + (len(self.buckets) - len(keep)),
            omitted_value=self.omitted_value
            + sum(bucket.value for bucket in self.buckets if bucket.key not in kept),
        )

    @property
    def bucket_sum(self) -> int:
        """Integer because every mode here counts studies; floats arrive with T10's metrics."""
        return int(sum(bucket.value for bucket in self.buckets))
