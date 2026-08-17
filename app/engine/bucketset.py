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
    omitted_value: float = 0.0
    """Value held by the categories the chart does not plot.

    The overlap arithmetic describes the *result*, not the chart, so it needs the full
    membership count even after narrowing. Without this, `bucket_sum` fell to the plotted
    categories while `with_value` still covered every study and the reported overlap went
    negative — nonsense in the one block whose purpose is auditable arithmetic.
    """
    omitted_buckets: int = 0
    """Categories the chart does not plot, so coverage can name a real number.

    Marking a truncated result `complete=False` was not enough on its own: the bucket set still
    held every key, so the note said "showing 10" for a chart drawing 3 and `bucket_sum` covered
    categories that were never rendered.
    """
    complete: bool = True
    """False when the bucket list was capped at `max_buckets` and labels were left out.

    Coverage arithmetic depends on it: a partition whose buckets do not reconcile is a bug worth
    warning about, but a partition showing 3 of 51,610 sponsors is *expected* not to reconcile,
    and warning that the difference is "unexplained" points at the data when the explanation is
    the caller's own `max_buckets`.
    """
    sample_size: int | None = None
    sample_coverage: float | None = None
    warnings: list[str] = field(default_factory=list)

    def plotted_only(self, kept: set[str]) -> BucketSet:
        """The same result narrowed to the categories the chart will actually draw."""
        keep = [bucket for bucket in self.buckets if bucket.key in kept]
        return replace(
            self,
            buckets=keep,
            omitted_buckets=len(self.buckets) - len(keep),
            omitted_value=self.omitted_value
            + sum(bucket.value for bucket in self.buckets if bucket.key not in kept),
            complete=self.complete and len(keep) == len(self.buckets),
        )

    @property
    def bucket_sum(self) -> int:
        """Integer because every mode here counts studies; floats arrive with T10's metrics."""
        return int(sum(bucket.value for bucket in self.buckets))
