"""Turning a `BucketSet` into the `Coverage` block. SPEC §4.3.

Coverage exists so a reader can tell an exact answer from an approximate one without knowing how
the engine works. Two rules follow from that and are enforced here rather than left to prose:

- an **overlapping** group-by states its actual numbers, because "these do not sum to the total"
  without the arithmetic is not disclosure, it is a disclaimer;
- a **partition** whose buckets do not reconcile emits a warning naming both numbers. That is a
  bug or an upstream surprise, and papering over it is how a wrong chart ships.

**Two different questions live here, and conflating them caused four bugs in a row.** The overlap
note describes the *result* — how a multi-valued field makes studies contribute more than once.
`bucket_sum` and the truncation note describe the *chart* — which categories a reader is actually
looking at. Truncation moves the second without moving the first, so the two quantities are named
apart (`result_memberships` versus `plotted_sum`) and each note is handed only the one it means.
An earlier version passed the full count into a parameter named `memberships` while the local of
the same name held the plotted one; the reported overlap went negative.
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
    plotted_sum = bucketset.bucket_sum
    result_memberships = plotted_sum + int(bucketset.omitted_value)
    with_value = bucketset.total - bucketset.unclassified
    # Two independent cuts, and conflating them was the cause of the last four bugs here.
    # `capped` means values were never counted, so nothing about them can be quoted. `narrowed`
    # means they were counted and then not drawn, so both the count and their memberships are
    # known. Only `capped` costs us the overlap arithmetic.
    capped = bucketset.aggregation_capped
    narrowed = bucketset.omitted_buckets > 0
    partial = capped or narrowed

    if not counts_studies:
        return (
            Coverage(
                aggregation_mode=bucketset.mode,
                groupby_semantics="partition" if dim.partition else "overlapping",
                bucket_sum=plotted_sum,
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
        if partial:
            overlap_note = _truncation_note(bucketset, dim)
        elif plotted_sum + bucketset.unclassified != bucketset.total:
            warnings.append(
                f"{dim.key} is a partition, so its buckets should sum to the total, but "
                f"{plotted_sum:,} bucket counts plus {bucketset.unclassified:,} unclassified "
                f"does not equal {bucketset.total:,}. The bucket counts are each exact; treat "
                f"the difference as unexplained rather than as rounding."
            )
    else:
        overlap_note = _overlap_note(
            dim,
            memberships=result_memberships,
            with_value=with_value,
            capped=capped,
            sampled=bucketset.sample_size is not None,
            warnings=warnings,
        )
        if partial:
            # Multi-valued dimensions take this branch, so without appending here a truncated
            # chart on phase or condition disclosed the cut nowhere in `meta.coverage` at all.
            overlap_note = f"{overlap_note} {_truncation_note(bucketset, dim)}"

    return (
        Coverage(
            aggregation_mode=bucketset.mode,
            groupby_semantics="partition" if dim.partition else "overlapping",
            bucket_sum=plotted_sum,
            unclassified_count=bucketset.unclassified,
            overlap_note=overlap_note,
            sample_size=bucketset.sample_size,
            sample_coverage=bucketset.sample_coverage,
        ),
        warnings,
    )


def _truncation_note(bucketset: BucketSet, dim: Dimension) -> str:
    """States what the chart draws against what is known, for either cut or both together.

    The two cuts compose, which the previous version assumed away: a fan-out that stopped at
    `max_buckets` and an axis then narrowed for plotting both leave marks, and reading only
    `omitted_buckets` printed "3 of 10" for a result whose true category count is unknown.
    """
    shown = len(bucketset.buckets)
    tail = (
        f"bucket_sum covers only the {shown:,} plotted. Each count shown is exact and they are "
        f"not expected to sum to the {bucketset.total:,} matching studies."
    )
    counted = shown + bucketset.omitted_buckets

    if bucketset.aggregation_capped and bucketset.omitted_buckets:
        return (
            f"Showing {shown:,} of {counted:,} {dim.key} values counted, and the aggregation "
            f"itself stopped at options.max_buckets, so further values exist whose number is "
            f"unknown; {tail}"
        )
    if bucketset.aggregation_capped:
        return (
            f"Showing {shown:,} {dim.key} values; further values exist but were cut by "
            f"options.max_buckets before they were counted, so their number is unknown. {tail}"
        )
    # Deliberately does not name a cause. On the cross-tab path the dropped categories may be
    # ones that paired with no secondary value rather than ones the cap cut, and naming
    # options.max_buckets there is the same wrong-cause failure as the empty-cross-tab case.
    return (
        f"Showing {shown:,} of {counted:,} {dim.key} values counted; the rest are not plotted "
        f"in this chart. {tail}"
    )


def _overlap_note(
    dim: Dimension,
    *,
    memberships: int,
    with_value: int,
    capped: bool,
    sampled: bool,
    warnings: list[str],
) -> str:
    """SPEC §4.3's exact shape, with the integers computed rather than described.

    A zero overlap is stated explicitly instead of omitting the note: on a multi-valued field,
    "they happen to sum this time" is itself information, and a missing note reads as a
    partition.
    """
    overlap = memberships - with_value
    # The upstream field name, so a reader can go and check: `phases` for the phase dimension.
    field = dim.record_path.rsplit(".", 1)[-1]

    if capped:
        # Values beyond the cap were never counted, so their memberships do not exist to be
        # added: the difference is not an overlap, it is the missing part. Printing it as one
        # produced "overlap -606", and the zero branch would have manufactured "no study carries
        # more than one phase" — a claim about the data invented by truncation.
        #
        # Narrowing alone does NOT land here: those categories were counted and `omitted_value`
        # kept their memberships, so the overlap stays exact and a disclaimer would be a
        # regression from disclosure.
        return (
            f"{field} is multi-valued, so buckets overlap and do not sum to the total. The "
            f"overlap cannot be quantified because the aggregation stopped at "
            f"options.max_buckets: {memberships:,} memberships were counted across the full "
            f"result, against {with_value:,} studies carrying a value."
        )

    if overlap < 0 and sampled:
        # A sample can simply never meet some corpus labels, while `with_value` is computed over
        # the whole corpus — so memberships falling short is the expected outcome of sampling,
        # not a fault. Blaming upstream here told a reader to distrust exact confirmed counts
        # because the sampler had not seen everything.
        return (
            f"{field} is multi-valued, so buckets overlap and do not sum to the total. The "
            f"overlap cannot be quantified because the labels came from a sample: "
            f"{memberships:,} memberships were counted across the labels confirmed, against "
            f"{with_value:,} studies carrying a value."
        )

    if overlap < 0:
        # Every value was counted and it still does not add up, so this is upstream disagreeing
        # with itself, which is exactly the case the partition branch warns about.
        warnings.append(
            f"{dim.key} counted {memberships:,} bucket memberships across a complete bucket "
            f"list, fewer than the {with_value:,} studies reported as carrying a value. The "
            f"per-bucket counts and the MISSING probe disagree; treat both as suspect."
        )
        return (
            f"{field} is multi-valued, so buckets overlap and do not sum to the total. "
            f"{memberships:,} memberships were counted against {with_value:,} studies carrying "
            f"a value, which does not reconcile; see meta.warnings."
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
