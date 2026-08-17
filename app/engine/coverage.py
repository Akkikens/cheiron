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
    # The overlap note describes the result; `bucket_sum` describes the chart. Narrowing the
    # bucket set to the plotted categories moved one and not the other, so the two must be
    # separated explicitly or the reported overlap goes negative.
    all_memberships = memberships + int(bucketset.omitted_value)
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
            overlap_note = _truncation_note(bucketset, dim)
        elif memberships + bucketset.unclassified != bucketset.total:
            warnings.append(
                f"{dim.key} is a partition, so its buckets should sum to the total, but "
                f"{memberships:,} bucket counts plus {bucketset.unclassified:,} unclassified "
                f"does not equal {bucketset.total:,}. The bucket counts are each exact; treat "
                f"the difference as unexplained rather than as rounding."
            )
    else:
        overlap_note = _overlap_note(
            dim, bucketset, memberships=all_memberships, with_value=with_value
        )
        if not bucketset.complete:
            # A multi-valued dimension takes this branch, so without this a truncated chart on
            # phase or condition disclosed the cut nowhere in `meta.coverage` at all.
            overlap_note = f"{overlap_note} {_truncation_note(bucketset, dim)}"

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


def _truncation_note(bucketset: BucketSet, dim: Dimension) -> str:
    """States what the chart draws against what the result held, in real numbers.

    `bucket_sum` here covers only the plotted categories, because the bucket set was narrowed
    before coverage was built — a sum over bars nobody can see is not a disclosure.
    """
    shown = len(bucketset.buckets)
    tail = (
        f"bucket_sum covers only the {shown:,} plotted. Each count shown is exact and they are "
        f"not expected to sum to the {bucketset.total:,} matching studies."
    )

    if bucketset.omitted_buckets:
        # The chart's shared axis was capped, so both numbers are known.
        present = shown + bucketset.omitted_buckets
        return (
            f"Showing {shown:,} of {present:,} {dim.key} values, the rest cut by "
            f"options.max_buckets; {tail}"
        )

    # The aggregation itself stopped at max_buckets, so the values beyond the cap were never
    # counted and there is no denominator to quote. "Showing 3 of 3, the rest cut" — which this
    # said when the cap bit during the fan-out rather than at the axis — is a contradiction.
    return (
        f"Showing {shown:,} {dim.key} values; further values exist but were cut by "
        f"options.max_buckets before they were counted, so their number is unknown. {tail}"
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

    if not bucketset.complete or overlap < 0:
        # With an incomplete bucket list the memberships counted fall short of the studies that
        # have a value, so their difference is not an overlap — it is the part that was never
        # counted. Printing it as one produced "overlap -606"; asserting the zero branch instead
        # would have manufactured "no study carries more than one phase", a claim about the data
        # invented by truncation.
        return (
            f"{field} is multi-valued, so buckets overlap and do not sum to the total. The "
            f"overlap cannot be quantified here because the bucket list is incomplete: "
            f"{memberships:,} memberships were counted across the buckets present, against "
            f"{with_value:,} studies carrying a value."
        )

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
