"""Turning a `BucketSet` into the `Coverage` block. SPEC §4.3.

Coverage exists so a reader can tell an exact answer from an approximate one without knowing how
the engine works. Two rules follow from that and are enforced here rather than left to prose:

- an **overlapping** group-by states its actual numbers, because "these do not sum to the total"
  without the arithmetic is not disclosure, it is a disclaimer;
- a **partition** whose buckets do not reconcile emits a warning naming both numbers. That is a
  bug or an upstream surprise, and papering over it is how a wrong chart ships.
"""

from __future__ import annotations

from app.engine.bucketset import BucketSet
from app.engine.dimensions import Dimension
from app.models.response import Coverage


def build_coverage(
    bucketset: BucketSet, dim: Dimension, *, counts_studies: bool = True
) -> tuple[Coverage, list[str]]:
    """Return the coverage block and any warnings the reconciliation turned up.

    `counts_studies` is false for enrollment metrics, where `bucket_sum` is a sum of *people*
    while `total` and `unclassified` are studies. Reconciling those is not just meaningless, it
    is actively misleading: every enrollment query used to emit "1,204,331 bucket counts plus 12
    unclassified does not equal 1,840 - treat the difference as unexplained", which reads as a
    data fault rather than as two different units.
    """
    warnings: list[str] = []
    memberships = bucketset.bucket_sum
    with_value = bucketset.total - bucketset.unclassified

    if not counts_studies:
        return (
            Coverage(
                aggregation_mode=bucketset.mode,
                groupby_semantics="partition" if dim.partition else "overlapping",
                bucket_sum=memberships,
                unclassified_count=bucketset.unclassified,
                overlap_note=(
                    f"Bucket values are enrollment totals, not study counts, so they are not "
                    f"comparable with total_matching_studies ({bucketset.total:,} studies). "
                    f"{bucketset.unclassified:,} studies have no value for {dim.key} and are "
                    f"excluded."
                ),
                sample_size=bucketset.sample_size,
                sample_coverage=bucketset.sample_coverage,
            ),
            warnings,
        )

    if dim.partition:
        overlap_note = None
        if not bucketset.complete:
            overlap_note = (
                f"Showing {len(bucketset.buckets)} of the {dim.key} values present; the rest were "
                f"cut by options.max_buckets. Each count shown is exact, and they are not "
                f"expected to sum to the {bucketset.total:,} matching studies."
            )
        elif memberships + bucketset.unclassified != bucketset.total:
            warnings.append(
                f"{dim.key} is a partition, so its buckets should sum to the total, but "
                f"{memberships:,} bucket counts plus {bucketset.unclassified:,} unclassified "
                f"does not equal {bucketset.total:,}. The bucket counts are each exact; treat "
                f"the difference as unexplained rather than as rounding."
            )
    else:
        overlap_note = _overlap_note(dim, bucketset, memberships=memberships, with_value=with_value)

    return (
        Coverage(
            aggregation_mode=bucketset.mode,
            groupby_semantics="partition" if dim.partition else "overlapping",
            bucket_sum=memberships,
            unclassified_count=bucketset.unclassified,
            overlap_note=overlap_note,
            sample_size=bucketset.sample_size,
            sample_coverage=bucketset.sample_coverage,
        ),
        warnings,
    )


def _overlap_note(
    dim: Dimension, bucketset: BucketSet, *, memberships: int, with_value: int
) -> str:
    """SPEC §4.3's exact shape, with the integers computed rather than described.

    A zero overlap is stated explicitly instead of omitting the note: on a multi-valued field,
    "they happen to sum this time" is itself information, and a missing note reads as a
    partition.
    """
    overlap = memberships - with_value
    # The upstream field name, so a reader can go and check: `phases` for the phase dimension.
    field = dim.record_path.rsplit(".", 1)[-1]

    if overlap == 0:
        return (
            f"{field} is multi-valued, but no study in this result set carries more than one "
            f"{dim.key}: {with_value:,} studies contribute {memberships:,} bucket memberships "
            f"(overlap 0). Bucket counts are each exact; that they sum to the total here is a "
            f"property of this result set, not of the dimension."
        )

    return (
        f"{field} is multi-valued: {with_value:,} studies carry \u22651 {dim.key} and contribute "
        f"{memberships:,} bucket memberships (overlap {overlap:,}). Bucket counts are each "
        f"exact; they do not sum to the total."
    )
