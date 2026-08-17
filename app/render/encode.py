"""Turn a `BucketSet` into a `Visualization`. SPEC §6.2, §4.1.

Titles are format strings over the plan — never model prose. The Other rollup is the one place
this module can silently drop data, so the annotation names both the rolled-in category count
and the summed value; a bare omission would be `truncated: true` wearing a different hat.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.ctg.vocab import Vocabulary
from app.engine.bucketset import Bucket, BucketSet
from app.engine.context import RunContext
from app.engine.dimensions import Dimension
from app.models.plan import AnalysisPlan, ChartType, Metric
from app.models.response import Visualization

# Minimal ISO-3166 alpha-3 map for names ClinicalTrials.gov actually returns. Unmapped names
# force a table fallback rather than a choropleth with a silent hole.
_ISO_A3: dict[str, str] = {
    "United States": "USA",
    "Canada": "CAN",
    "Mexico": "MEX",
    "United Kingdom": "GBR",
    "France": "FRA",
    "Germany": "DEU",
    "Italy": "ITA",
    "Spain": "ESP",
    "China": "CHN",
    "Japan": "JPN",
    "Korea, Republic of": "KOR",
    "India": "IND",
    "Brazil": "BRA",
    "Australia": "AUS",
    "Netherlands": "NLD",
    "Belgium": "BEL",
    "Switzerland": "CHE",
    "Sweden": "SWE",
    "Poland": "POL",
    "Israel": "ISR",
    "Taiwan": "TWN",
    "Russian Federation": "RUS",
    "Turkey": "TUR",
    "Argentina": "ARG",
    "South Africa": "ZAF",
    "Denmark": "DNK",
    "Norway": "NOR",
    "Finland": "FIN",
    "Austria": "AUT",
    "Ireland": "IRL",
    "New Zealand": "NZL",
    "Singapore": "SGP",
    "Hong Kong": "HKG",
    "Thailand": "THA",
    "Egypt": "EGY",
    "Greece": "GRC",
    "Portugal": "PRT",
    "Czechia": "CZE",
    "Hungary": "HUN",
    "Chile": "CHL",
    "Colombia": "COL",
    "Peru": "PER",
    "Niger": "NER",
    "Nigeria": "NGA",
    "Guinea": "GIN",
}


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
                type=ChartType.TABLE if chart_type is not ChartType.KPI else ChartType.KPI,
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

    buckets, rollup_annotation = _maybe_rollup(bucketset.buckets, ctx.options.max_buckets)
    rows = [_row(bucket, dim, plan.metric, ctx.vocab) for bucket in buckets]
    if chart_type is ChartType.GROUPED_BAR_CHART:
        # Network A7 downgrade and comparison charts both need this channel; single-series
        # paths get a constant so every row still satisfies SPEC §4.1.
        default_series = plan.series[0].label if plan.series else "all"
        for row in rows:
            row.setdefault("series", default_series)
    if chart_type is ChartType.STACKED_BAR_CHART:
        for row in rows:
            row.setdefault("stack", "all")
    rows = _sort_rows(rows, dim, ctx.vocab, chart_type)
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
    rows: list[dict[str, Any]], dim: Dimension, vocab: Vocabulary, chart_type: ChartType
) -> list[dict[str, Any]]:
    key = dim.key
    metric_field = next(
        (
            field
            for field in rows[0]
            if field.endswith("_count") or field.endswith("_sum") or field.endswith("_median")
        ),
        "study_count",
    )

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
    if chart_type is ChartType.KPI:
        return {
            "value": {
                "field": metric.value,
                "type": "quantitative",
                "label": _metric_label(metric),
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

    if unmapped:
        warnings.append(
            f"{len(unmapped)} country name(s) have no ISO-3166 alpha-3 mapping and were "
            f"omitted from the choropleth: {', '.join(unmapped[:5])}"
            + ("…" if len(unmapped) > 5 else "")
            + "; returning a table of all countries instead."
        )
        # Full table fallback with every bucket, including unmapped.
        table_type = ChartType.TABLE
        viz, more = render(
            plan,
            bucketset,
            table_type,
            dim,
            ctx,
        )
        return viz, warnings + more

    return (
        Visualization(
            type=ChartType.CHOROPLETH_MAP,
            title=_title(plan, dim),
            subtitle=_subtitle(bucketset, ctx),
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
                },
            },
            data=rows,
        ),
        warnings,
    )


def _title(plan: AnalysisPlan, dim: Dimension) -> str:
    subject = plan.filters.intervention or plan.filters.condition or plan.filters.sponsor
    if subject:
        return f"{_title_case(subject)} Trials by {_dimension_noun(dim)}"
    return f"Clinical Trials by {_dimension_noun(dim)}"


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


def _metric_label(metric: Metric) -> str:
    return {
        Metric.STUDY_COUNT: "Number of trials",
        Metric.ENROLLMENT_SUM: "Total enrollment",
        Metric.ENROLLMENT_MEDIAN: "Median enrollment",
    }[metric]
