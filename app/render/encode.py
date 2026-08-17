"""Turn a `BucketSet` into a `Visualization`. SPEC §6.2, §4.1.

Titles are format strings over the plan — never model prose. The Other rollup is the one place
this module can silently drop data, so the annotation names both the rolled-in category count
and the summed value; a bare omission would be `truncated: true` wearing a different hat.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.ctg.vocab import Vocabulary
from app.engine.bucketset import Bucket, BucketSet
from app.engine.citations import nct_id_of, value_at
from app.engine.context import RunContext
from app.engine.dimensions import ENROLLMENT_BINS, Dimension, bin_label
from app.engine.multi import CrossCell, Panel
from app.models.plan import AnalysisPlan, ChartType, Metric
from app.models.response import Visualization
from app.render.countries import ISO_A3

# Minimal ISO-3166 alpha-3 map for names ClinicalTrials.gov actually returns. Unmapped names
# force a table fallback rather than a choropleth with a silent hole.
_ISO_A3 = ISO_A3
"""Generated from the corpus by `scripts/build_country_map.py` (226 of 226 names)."""


def render(
    plan: AnalysisPlan,
    bucketset: BucketSet,
    chart_type: ChartType,
    dim: Dimension,
    ctx: RunContext,
) -> tuple[Visualization, list[str]]:
    """Build the visualization. Returns warnings for choropleth fallbacks and empty results."""
    warnings: list[str] = []

    if bucketset.total == 0 or not bucketset.buckets:
        warnings.append(
            "No studies matched these filters; returning an empty visualization rather than "
            "a fabricated row."
        )
        return (
            Visualization(
                # Keep the shape the caller asked for where the empty form is still valid; only
                # the row-shaped charts collapse to a table. Returning TABLE with {nodes, edges}
                # would fail the response model's own encoding check and turn a legitimate empty
                # answer into a 500.
                type=chart_type
                if chart_type in (ChartType.KPI, ChartType.NETWORK_GRAPH)
                else ChartType.TABLE,
                title=_title(plan, dim),
                subtitle=_subtitle(bucketset, ctx),
                encoding=_empty_encoding(chart_type, dim),
                data=[]
                if chart_type is not ChartType.NETWORK_GRAPH
                else {"nodes": [], "edges": []},
            ),
            warnings,
        )

    if chart_type is ChartType.CHOROPLETH_MAP:
        return _choropleth(plan, bucketset, dim, ctx, warnings)

    if chart_type is ChartType.HISTOGRAM:
        # No Other rollup on a histogram. Bins are contiguous and there are only seven, so a
        # rollup produces an "OTHER" bar with no edges of its own — `_bin_edges` would hand it
        # [0, ∞), a full-width bar overlapping every real one.
        buckets, rollup_annotation = list(bucketset.buckets), None
    else:
        buckets, rollup_annotation = _maybe_rollup(bucketset.buckets, ctx.options.max_buckets)
    rows = [_row(bucket, dim, plan.metric, ctx.vocab) for bucket in buckets]
    if chart_type is ChartType.HISTOGRAM:
        # A histogram bar spans a range, so the renderer needs its edges, not just its label.
        for row in rows:
            low, high = _bin_edges(str(row[dim.key]))
            row["bin_start"] = low
            row["bin_end"] = high
    if chart_type in (ChartType.GROUPED_BAR_CHART, ChartType.STACKED_BAR_CHART):
        # Reached only when there is genuinely one series and no secondary dimension — the
        # network A7 downgrade is the main case. Real comparisons and cross-tabs never arrive
        # here; they go through `render_panels` / `render_crosstab`, which carry real breakdowns.
        # A constant is honest for a single series and dishonest for several, which is why the
        # multi-series paths are separate functions rather than a flag on this one.
        channel = "series" if chart_type is ChartType.GROUPED_BAR_CHART else "stack"
        for row in rows:
            row.setdefault(channel, "all")
    rows = _sort_rows(rows, dim, ctx.vocab, chart_type, plan.metric)
    rows = _other_last(rows, dim.key)

    encoding = _encoding(chart_type, dim, plan.metric, ctx.vocab, rows)
    annotations: list[dict[str, Any]] = []
    if rollup_annotation is not None:
        annotations.append(rollup_annotation)
    if bucketset.semantics == "overlapping":
        annotations.append({"type": "note", "text": "Buckets overlap; see meta.coverage"})

    return (
        Visualization(
            type=chart_type,
            title=_title(plan, dim),
            subtitle=_subtitle(bucketset, ctx),
            encoding=encoding,
            data=rows,
            annotations=annotations or None,
        ),
        warnings,
    )


def plotted_axis_keys(panels: Sequence[Panel], max_buckets: int) -> set[str]:
    """The axis categories a comparison will actually draw. `analyze` asks before rendering.

    Coverage is built from the merged bucket set, which holds every key, so it has to be
    narrowed to these before the numbers in `meta.coverage` describe the chart in front of you.
    """
    return _top_keys_across(panels, max_buckets)


def plotted_crosstab_keys(cells: Sequence[CrossCell], max_buckets: int) -> set[str]:
    return _top_primary_keys(cells, max_buckets)


def render_panels(
    plan: AnalysisPlan,
    panels: Sequence[Panel],
    merged: BucketSet,
    dim: Dimension,
    ctx: RunContext,
) -> tuple[Visualization, list[str]]:
    """A real comparison: one row per (series, bucket), each count from that series' own fan-out.

    Every row carries the label of the series it was actually counted under. The previous
    behaviour — one series' label stamped on the base filter's counts — is the failure this
    function exists to make impossible.
    """
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []

    # `max_buckets` is a limit on categories on the axis, not on rows: N series share one axis,
    # so the cap applies to the union of their keys and every series keeps its bar for a kept
    # category. Ignoring it entirely let a lead_sponsor comparison emit thousands of rows.
    keys = _top_keys_across(panels, ctx.options.max_buckets)
    dropped = _dropped_key_count(panels, keys)

    for panel in panels:
        if not panel.bucketset.buckets:
            warnings.append(f"Series {panel.label!r} matched no studies and has no bars.")
        for bucket in panel.bucketset.buckets:
            if bucket.key not in keys:
                continue
            row = _row(bucket, dim, plan.metric, ctx.vocab)
            row["series"] = panel.label
            rows.append(row)

    rows = _sort_rows(rows, dim, ctx.vocab, ChartType.GROUPED_BAR_CHART, plan.metric)

    annotations: list[dict[str, Any]] = []
    if dropped:
        annotations.append(
            {
                "type": "rollup",
                "text": (
                    f"Showing the top {len(keys)} {dim.key} values across all series; "
                    f"{dropped} further value(s) are not plotted because options.max_buckets "
                    f"is {ctx.options.max_buckets}."
                ),
                "rolled_categories": dropped,
            }
        )
    annotations.append(
        {
            "type": "series",
            "text": "Each series is an independent query; counts are exact within a series.",
            "series": [
                {"label": panel.label, "total_matching_studies": panel.bucketset.total}
                for panel in panels
            ],
        }
    )
    if merged.semantics == "overlapping":
        annotations.append({"type": "note", "text": "Buckets overlap; see meta.coverage"})

    return (
        Visualization(
            type=ChartType.GROUPED_BAR_CHART,
            title=f"{_title(plan, dim)} ({' vs '.join(panel.label for panel in panels)})",
            subtitle=_subtitle(merged, ctx),
            encoding=_encoding(ChartType.GROUPED_BAR_CHART, dim, plan.metric, ctx.vocab, rows),
            data=rows,
            annotations=annotations,
        ),
        warnings,
    )


def render_crosstab(
    plan: AnalysisPlan,
    cells: Sequence[CrossCell],
    bucketset: BucketSet,
    dim: Dimension,
    secondary: Dimension,
    ctx: RunContext,
    chart_type: ChartType,
) -> tuple[Visualization, list[str]]:
    """A real cross-tab: one row per (primary, secondary) cell with its own count.

    Stacking is only offered when the **secondary** dimension partitions (SPEC §6.1) — stacking
    a multi-valued dimension implies segments that sum to the bar when they do not.
    """
    warnings: list[str] = []
    channel = "stack" if chart_type is ChartType.STACKED_BAR_CHART else "series"

    # Same rule as a comparison: cap the axis, not the cells. A condition-by-phase cross-tab over
    # a full record page produces thousands of cells otherwise, and the caller asked for a limit.
    kept_primary = _top_primary_keys(cells, ctx.options.max_buckets)
    hidden = {cell.primary for cell in cells} - kept_primary

    rows: list[dict[str, Any]] = []
    for cell in cells:
        if cell.primary not in kept_primary:
            continue
        rows.append(
            {
                dim.key: cell.primary,
                f"{dim.key}_label": _label_for(dim, cell.primary, ctx.vocab),
                channel: _label_for(secondary, cell.secondary, ctx.vocab),
                f"{channel}_key": cell.secondary,
                plan.metric.value: cell.value,
                "exactness": "exact",
            }
        )

    rows = _sort_rows(rows, dim, ctx.vocab, chart_type, plan.metric)

    encoding = _encoding(chart_type, dim, plan.metric, ctx.vocab, rows)
    encoding[channel] = {
        "field": channel,
        "type": "nominal",
        "label": secondary.label,
    }

    annotations: list[dict[str, Any]] = []
    if hidden:
        annotations.append(
            {
                "type": "rollup",
                "text": (
                    f"Showing the top {len(kept_primary)} {dim.key} values by total; "
                    f"{len(hidden)} further value(s) are not plotted because options.max_buckets "
                    f"is {ctx.options.max_buckets}."
                ),
                "rolled_categories": len(hidden),
            }
        )
    annotations.append(
        {
            "type": "crosstab",
            "text": (
                f"Each cell is an exact count of studies matching both the {dim.label.lower()} "
                f"and the {secondary.label.lower()}."
            ),
            "cells": len(rows),
        }
    )
    if not secondary.partition:
        annotations.append(
            {
                "type": "note",
                "text": (
                    f"{secondary.label} is multi-valued, so a study can appear in more than one "
                    f"{channel}; segments do not sum to their bar."
                ),
            }
        )

    return (
        Visualization(
            type=chart_type,
            title=f"{_title(plan, dim)} by {secondary.label}",
            subtitle=_subtitle(bucketset, ctx),
            encoding=encoding,
            data=rows,
            annotations=annotations,
        ),
        warnings,
    )


def _bin_edges(key: str) -> tuple[int, int | None]:
    for low, high in ENROLLMENT_BINS:
        if bin_label(low, high) == key:
            return low, high
    return 0, None


def render_scatter(
    plan: AnalysisPlan,
    studies: Sequence[Mapping[str, Any]],
    bucketset: BucketSet,
    dim: Dimension,
    ctx: RunContext,
) -> tuple[Visualization, list[str]]:
    """One point per study: start year against enrollment. `complete_records` only.

    Every point is a real study with its NCT id attached, so a reader can open any outlier and
    check it. Studies missing either axis are excluded and counted in the annotation rather than
    plotted at zero, which would manufacture a cluster on both axes that does not exist.
    """
    warnings: list[str] = []
    points: list[dict[str, Any]] = []
    skipped = 0

    for record in studies:
        year = _study_year(record)
        enrollment = _study_enrollment(record)
        nct = _study_nct(record)
        if year is None or enrollment is None or nct is None:
            skipped += 1
            continue
        points.append(
            {
                "nct_id": nct,
                "start_year": year,
                "enrollment": enrollment,
                "url": f"https://clinicaltrials.gov/study/{nct}",
            }
        )

    points.sort(key=lambda point: (point["start_year"], point["nct_id"]))

    annotations: list[dict[str, Any]] = [
        {
            "type": "points",
            "text": (
                f"{len(points):,} of {len(studies):,} studies plotted; {skipped:,} lack a start "
                f"date or an enrollment count and are excluded rather than plotted at zero."
            ),
            "plotted": len(points),
            "excluded": skipped,
        }
    ]

    return (
        Visualization(
            type=ChartType.SCATTER_PLOT,
            # Not `_title(plan, dim)`: that appends "by Enrollment", giving "… by Enrollment:
            # enrollment by start year". A scatter names both axes itself.
            title=f"{_subject_title(plan)}: enrollment by start year",
            subtitle=_subtitle(bucketset, ctx),
            encoding={
                "x": {"field": "start_year", "type": "temporal", "label": "Start year"},
                "y": {"field": "enrollment", "type": "quantitative", "label": "Enrollment"},
                "color": {"field": "nct_id", "type": "nominal", "label": "Study"},
            },
            data=points,
            annotations=annotations,
        ),
        warnings,
    )


def _study_year(record: Mapping[str, Any]) -> int | None:
    try:
        raw = value_at(record, "protocolSection.statusModule.startDateStruct.date")
    except KeyError:
        return None
    if not isinstance(raw, str) or len(raw) < 4 or not raw[:4].isdigit():
        return None
    year = int(raw[:4])
    return year if 1900 <= year <= 2100 else None


def _study_enrollment(record: Mapping[str, Any]) -> int | None:
    try:
        raw = value_at(record, "protocolSection.designModule.enrollmentInfo.count")
    except KeyError:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


def _study_nct(record: Mapping[str, Any]) -> str | None:
    try:
        return nct_id_of(record)
    except KeyError:
        return None


def _top_keys_across(panels: Sequence[Panel], max_buckets: int) -> set[str]:
    """The axis categories a multi-series chart keeps, ranked by total value across series."""
    totals: dict[str, float] = {}
    for panel in panels:
        for bucket in panel.bucketset.buckets:
            totals[bucket.key] = totals.get(bucket.key, 0.0) + bucket.value
    ranked = sorted(totals, key=lambda key: (-totals[key], key))
    return set(ranked[:max_buckets])


def _dropped_key_count(panels: Sequence[Panel], kept: set[str]) -> int:
    every = {bucket.key for panel in panels for bucket in panel.bucketset.buckets}
    return len(every - kept)


def _top_primary_keys(cells: Sequence[CrossCell], max_buckets: int) -> set[str]:
    totals: dict[str, int] = {}
    for cell in cells:
        totals[cell.primary] = totals.get(cell.primary, 0) + cell.value
    ranked = sorted(totals, key=lambda key: (-totals[key], key))
    return set(ranked[:max_buckets])


def _label_for(dim: Dimension, key: str, vocab: Vocabulary) -> str:
    return key if dim.enum_name is None else vocab.label(dim.enum_name, key)


def _maybe_rollup(
    buckets: list[Bucket], max_buckets: int
) -> tuple[list[Bucket], dict[str, Any] | None]:
    """Keep the top `max_buckets - 1` by value; roll the rest into Other.

    The annotation names both N and the summed value. Omitting either is how a silent drop
    dresses itself as disclosure.
    """
    if len(buckets) <= max_buckets:
        return list(buckets), None

    ordered = sorted(buckets, key=lambda b: (-b.value, b.key))
    kept = ordered[: max_buckets - 1]
    rolled = ordered[max_buckets - 1 :]
    rolled_sum = sum(bucket.value for bucket in rolled)
    other = Bucket(
        key="OTHER",
        label=f"Other ({len(rolled)} categories)",
        value=rolled_sum,
        exactness="exact",
    )
    annotation = {
        "type": "rollup",
        "text": (f"Other rolls up {len(rolled)} categories totalling {int(rolled_sum):,} studies."),
        "rolled_categories": len(rolled),
        "rolled_value": int(rolled_sum),
    }
    return [*kept, other], annotation


def _row(bucket: Bucket, dim: Dimension, metric: Metric, vocab: Vocabulary) -> dict[str, Any]:
    metric_field = metric.value
    row: dict[str, Any] = {
        dim.key: bucket.key,
        f"{dim.key}_label": bucket.label,
        metric_field: int(bucket.value) if float(bucket.value).is_integer() else bucket.value,
        "exactness": bucket.exactness,
    }
    if bucket.citations:
        row["citations"] = [c.model_dump() for c in bucket.citations]
        if bucket.citation_note is not None:
            row["citation_note"] = bucket.citation_note
    return row


def _sort_rows(
    rows: list[dict[str, Any]],
    dim: Dimension,
    vocab: Vocabulary,
    chart_type: ChartType,
    metric: Metric = Metric.STUDY_COUNT,
) -> list[dict[str, Any]]:
    key = dim.key
    # The metric is known from the plan, so it is passed in rather than sniffed out of the row
    # keys. Sniffing for a field ending in "_count" picked the *dimension* on an
    # `enrollment_count` group-by and tried to sort bin labels as floats.
    metric_field = metric.value

    if chart_type is ChartType.HISTOGRAM:
        # Bins are ordered by their lower edge, never by height: a histogram whose bars are
        # sorted by count is a bar chart wearing a histogram's axis.
        order = {bin_label(low, high): index for index, (low, high) in enumerate(ENROLLMENT_BINS)}
        return sorted(rows, key=lambda row: order.get(str(row[key]), len(order)))

    if chart_type is ChartType.TIME_SERIES:
        return sorted(rows, key=lambda row: str(row[key]))

    if dim.enum_name is not None:
        order = {value: index for index, value in enumerate(vocab.sort_order(dim.enum_name))}
        # OTHER and any unknown key sort after the clinical order; MISSING stays where sort_order
        # puts it when it is present as a row (it is not, after T06's intersection — but the
        # sentinel is still legitimate on this side of the boundary for display).
        return sorted(
            rows,
            key=lambda row: (
                0 if row[key] in order else 1,
                order.get(row[key], 0),
                -float(row[metric_field]),
                str(row[key]),
            ),
        )

    # Open vocabulary: value descending, key ascending for stability.
    return sorted(rows, key=lambda row: (-float(row[metric_field]), str(row[key])))


def _other_last(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """OTHER is a rollup residue, not a peer category — it always trails the axis."""
    kept = [row for row in rows if row.get(key) != "OTHER"]
    other = [row for row in rows if row.get(key) == "OTHER"]
    return kept + other


def _encoding(
    chart_type: ChartType,
    dim: Dimension,
    metric: Metric,
    vocab: Vocabulary,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    x_type = (
        "temporal"
        if chart_type is ChartType.TIME_SERIES
        else ("ordinal" if dim.enum_name == "Phase" else "nominal")
    )
    x_channel: dict[str, Any] = {
        "field": dim.key,
        "type": x_type,
        "label": dim.label,
    }
    if dim.enum_name is not None:
        # Include MISSING in the published sort array (SPEC §4's example) even when it is not a
        # data row — same sentinel, correct side of the query/render boundary.
        x_channel["sort"] = list(vocab.sort_order(dim.enum_name))

    y_channel = {
        "field": metric.value,
        "type": "quantitative",
        "label": _metric_label(metric),
        "unit": _metric_unit(metric),
    }

    if chart_type is ChartType.BAR_CHART:
        return {"x": x_channel, "y": y_channel}
    if chart_type is ChartType.GROUPED_BAR_CHART:
        return {
            "x": x_channel,
            "y": y_channel,
            "series": {"field": "series", "type": "nominal", "label": "Series"},
        }
    if chart_type is ChartType.STACKED_BAR_CHART:
        return {
            "x": x_channel,
            "y": y_channel,
            "stack": {"field": "stack", "type": "nominal", "label": "Stack"},
        }
    if chart_type is ChartType.TIME_SERIES:
        return {"x": x_channel, "y": y_channel}
    if chart_type is ChartType.HISTOGRAM:
        return {
            "x": {
                **x_channel,
                # `field` holds a bin *label* ("11-50"), so the channel is ordinal. The numbers
                # live in bin_start/bin_end; calling this quantitative would tell a renderer to
                # scale strings.
                "type": "ordinal",
                "bin_start": "bin_start",
                "bin_end": "bin_end",
                "sort": [bin_label(low, high) for low, high in ENROLLMENT_BINS],
            },
            "y": y_channel,
        }
    if chart_type is ChartType.KPI:
        return {
            "value": {
                "field": metric.value,
                "type": "quantitative",
                "label": _metric_label(metric),
                "unit": _metric_unit(metric),
            },
            "label": {"field": f"{dim.key}_label", "type": "nominal", "label": dim.label},
        }
    if chart_type is ChartType.TABLE:
        return {
            "columns": [
                x_channel,
                {"field": f"{dim.key}_label", "type": "nominal", "label": f"{dim.label} (label)"},
                y_channel,
                {"field": "exactness", "type": "nominal", "label": "Exactness"},
            ]
        }
    # Fallback encoding for types not yet fully wired.
    return {"x": x_channel, "y": y_channel}


def _empty_encoding(chart_type: ChartType, dim: Dimension) -> dict[str, Any]:
    if chart_type is ChartType.KPI:
        return {
            "value": {"field": "study_count", "type": "quantitative", "label": "Number of trials"},
            "label": {"field": f"{dim.key}_label", "type": "nominal", "label": dim.label},
        }
    if chart_type is ChartType.NETWORK_GRAPH:
        return {"nodes": {"id": "id"}, "edges": {"source": "source"}}
    return {
        "x": {"field": dim.key, "type": "nominal", "label": dim.label},
        "y": {"field": "study_count", "type": "quantitative", "label": "Number of trials"},
    }


CHOROPLETH_MIN_COVERAGE = 0.95
"""Draw the map when it represents at least this share of the result's studies.

Below it, the omission is large enough that the map would misrepresent the distribution, and a
table of every country is the honest answer.
"""


def _unmapped_counts(bucketset: BucketSet, unmapped: list[str]) -> list[tuple[str, str]]:
    by_key = {bucket.key: int(bucket.value) for bucket in bucketset.buckets}
    return [(name, f"{by_key.get(name, 0):,}") for name in unmapped[:5]]


def _choropleth(
    plan: AnalysisPlan,
    bucketset: BucketSet,
    dim: Dimension,
    ctx: RunContext,
    warnings: list[str],
) -> tuple[Visualization, list[str]]:
    rows: list[dict[str, Any]] = []
    unmapped: list[str] = []
    for bucket in bucketset.buckets:
        iso = _ISO_A3.get(bucket.key)
        if iso is None:
            unmapped.append(bucket.key)
            continue
        rows.append(
            {
                "country": bucket.key,
                "iso_a3": iso,
                "country_label": bucket.label,
                plan.metric.value: int(bucket.value),
                "exactness": bucket.exactness,
            }
        )

    mapped_value = sum(int(row[plan.metric.value]) for row in rows)
    total_value = sum(int(bucket.value) for bucket in bucketset.buckets)
    coverage = mapped_value / total_value if total_value else 1.0

    annotations: list[dict[str, Any]] = []
    if unmapped:
        # An all-or-nothing rule threw away a working map because one name in twenty did not
        # place — "South Korea", which ISO spells "Korea, Republic of". So the map is drawn when
        # it represents nearly all of the value, and what it could not place is named with its
        # count rather than quietly dropped. Below that, the table is the honest answer.
        detail = ", ".join(f"{name} ({fmt})" for name, fmt in _unmapped_counts(bucketset, unmapped))
        message = (
            f"{len(unmapped)} country name(s) have no ISO-3166 alpha-3 mapping: {detail}. "
            f"The map covers {coverage:.1%} of the studies in this result."
        )
        if coverage < CHOROPLETH_MIN_COVERAGE:
            warnings.append(message + " Returning a table of all countries instead.")
            viz, more = render(plan, bucketset, ChartType.TABLE, dim, ctx)
            return viz, warnings + more

        warnings.append(message + " They are excluded from the map and listed here.")
        annotations.append(
            {
                "type": "unmapped",
                "text": message,
                "countries": unmapped,
                "value_coverage": round(coverage, 4),
            }
        )

    return (
        Visualization(
            type=ChartType.CHOROPLETH_MAP,
            title=_title(plan, dim),
            subtitle=_subtitle(bucketset, ctx),
            annotations=annotations or None,
            encoding={
                "location": {
                    "field": "iso_a3",
                    "type": "geo",
                    "label": "Country",
                },
                "value": {
                    "field": plan.metric.value,
                    "type": "quantitative",
                    "label": _metric_label(plan.metric),
                    "unit": _metric_unit(plan.metric),
                },
            },
            data=rows,
        ),
        warnings,
    )


def _title(plan: AnalysisPlan, dim: Dimension) -> str:
    return f"{_subject_title(plan)} by {_dimension_noun(dim)}"


def _subject_title(plan: AnalysisPlan) -> str:
    subject = plan.filters.intervention or plan.filters.condition or plan.filters.sponsor
    return f"{_title_case(subject)} Trials" if subject else "Clinical Trials"


def _dimension_noun(dim: Dimension) -> str:
    """The dimension as it reads in a title, which is not how it reads on an axis.

    `dim.label` is written for an axis, where "Trial phase" is right. In a title it produces
    "Pembrolizumab Trials by Trial phase" — so the redundant qualifier is dropped and the result
    is title-cased. Titles are derived from the plan by code, never model-authored (SPEC §4.1),
    which is exactly why the wording has to be handled here rather than left to a prompt.
    """
    label = dim.label
    # Only "Trial " is redundant — the subject is already "… Trials". "Study type" keeps its
    # qualifier, because "Trials by Type" says less than "Trials by Study type".
    if label.startswith("Trial ") and len(label) > len("Trial "):
        label = label[len("Trial ") :]
    return label[:1].upper() + label[1:]


def _title_case(text: str) -> str:
    """Title-case without mangling already-correct drug names like Pembrolizumab."""
    if text[:1].isupper() and not text.isupper():
        return text
    return text.title()


def _subtitle(bucketset: BucketSet, ctx: RunContext) -> str:
    day = _date_of(ctx.data_timestamp)
    return f"{bucketset.total:,} studies · ClinicalTrials.gov, data as of {day}"


def _date_of(timestamp: str) -> str:
    """`2026-08-14T09:00:05` → `2026-08-14`."""
    return timestamp.split("T", 1)[0] if timestamp else date.today().isoformat()


def _metric_unit(metric: Metric) -> str:
    """Trials or people — the two metrics count different things and a bare number hides which."""
    return "studies" if metric is Metric.STUDY_COUNT else "participants"


def _metric_label(metric: Metric) -> str:
    return {
        Metric.STUDY_COUNT: "Number of trials",
        Metric.ENROLLMENT_SUM: "Total enrollment",
        Metric.ENROLLMENT_MEDIAN: "Median enrollment",
    }[metric]
