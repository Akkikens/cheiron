"""`BucketSet` — the single handoff from the engine into render (BUILD-PLAN §4)."""

from __future__ import annotations

from dataclasses import dataclass, field
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

    @property
    def bucket_sum(self) -> int:
        """Integer because every mode here counts studies; floats arrive with T10's metrics."""
        return int(sum(bucket.value for bucket in self.buckets))
