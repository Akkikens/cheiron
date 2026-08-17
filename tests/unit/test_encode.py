"""Visualization encoding: titles, sort order, axis truncation. SPEC §4.1, §6.2."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.engine.bucketset import Bucket, BucketSet
from app.engine.context import new_context
from app.engine.dimensions import REGISTRY
from app.models.plan import AnalysisPlan, ChartType, GroupBy, Intent, StudyFilter
from app.models.request import Options
from app.render.encode import render
from tests.conftest import Handler, stub_transport

DATA_TIMESTAMP = "2026-08-14T09:00:05"


@pytest.fixture
async def vocab(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


def a_plan(**overrides: Any) -> AnalysisPlan:
    base: dict[str, Any] = {
        "intent": Intent.DISTRIBUTION,
        "filters": StudyFilter(intervention="Pembrolizumab"),
        "group_by": GroupBy(dimension="phase"),
        "interpretation": "Distribution of clinical trials studying Pembrolizumab across phases.",
    }
    return AnalysisPlan(**{**base, **overrides})


def a_bucketset(buckets: list[Bucket], *, total: int | None = None) -> BucketSet:
    return BucketSet(
        buckets=buckets,
        total=total if total is not None else int(sum(b.value for b in buckets)),
        unclassified=0,
        semantics="overlapping",
        mode="server_counts",
    )


async def a_ctx(settings: Settings, vocab: Vocabulary, *, max_buckets: int = 20) -> Any:
    return new_context(
        CTGClient(stub_transport(settings, lambda r: __import__("httpx").Response(404))),
        vocab,
        Options(max_buckets=max_buckets),
        settings=settings,
        data_timestamp=DATA_TIMESTAMP,
    )


async def test_bar_chart_rows_carry_key_label_and_count(
    settings: Settings, vocab: Vocabulary
) -> None:
    ctx = await a_ctx(settings, vocab)
    buckets = [
        Bucket(key="PHASE2", label="Phase 2", value=1750, exactness="exact"),
        Bucket(key="PHASE1", label="Phase 1", value=1039, exactness="exact"),
    ]
    viz, _ = render(
        a_plan(), a_bucketset(buckets, total=2927), ChartType.BAR_CHART, REGISTRY["phase"], ctx
    )

    assert viz.type == ChartType.BAR_CHART
    assert viz.title == "Pembrolizumab Trials by Phase"
    assert viz.subtitle == "2,927 studies · ClinicalTrials.gov, data as of 2026-08-14"
    assert viz.encoding["x"]["field"] == "phase"
    assert viz.encoding["x"]["sort"][-1] == "MISSING"
    assert all("phase" in row and "phase_label" in row and "study_count" in row for row in viz.data)


async def test_phase_axis_is_clinically_ordered(settings: Settings, vocab: Vocabulary) -> None:
    ctx = await a_ctx(settings, vocab)
    buckets = [
        Bucket(key=key, label=key, value=float(i), exactness="exact")
        for i, key in enumerate(["NA", "PHASE3", "EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE4"])
    ]
    viz, _ = render(a_plan(), a_bucketset(buckets), ChartType.BAR_CHART, REGISTRY["phase"], ctx)

    assert [row["phase"] for row in viz.data] == [
        "EARLY_PHASE1",
        "PHASE1",
        "PHASE2",
        "PHASE3",
        "PHASE4",
        "NA",
    ]


async def test_encode_plots_every_bucket_it_is_given(settings: Settings, vocab: Vocabulary) -> None:
    """Truncation belongs to the aggregation mode, which is the only layer that can disclose it.

    This module used to roll a long tail into a synthesized "Other" category. Every mode already
    caps at `max_buckets`, so the branch was unreachable, and reaching it would have put a bar on
    the chart that `meta.coverage.bucket_sum` does not account for.
    """
    ctx = await a_ctx(settings, vocab, max_buckets=20)
    buckets = [
        Bucket(key=f"S{i:02d}", label=f"Sponsor {i}", value=float(100 - i), exactness="exact")
        for i in range(50)
    ]
    viz, _ = render(
        a_plan(group_by=GroupBy(dimension="lead_sponsor")),
        a_bucketset(buckets),
        ChartType.TABLE,
        REGISTRY["lead_sponsor"],
        ctx,
    )

    assert len(viz.data) == 50
    assert "OTHER" not in {row["lead_sponsor"] for row in viz.data}


async def test_zero_results_are_empty_not_fabricated(settings: Settings, vocab: Vocabulary) -> None:
    ctx = await a_ctx(settings, vocab)
    viz, warnings = render(
        a_plan(),
        a_bucketset([], total=0),
        ChartType.BAR_CHART,
        REGISTRY["phase"],
        ctx,
    )

    assert viz.data == []
    assert any("empty" in warning.lower() or "No studies" in warning for warning in warnings)


async def test_open_vocab_sorts_by_value_then_key(settings: Settings, vocab: Vocabulary) -> None:
    ctx = await a_ctx(settings, vocab)
    buckets = [
        Bucket(key="Pfizer", label="Pfizer", value=10, exactness="exact"),
        Bucket(key="Merck", label="Merck", value=20, exactness="exact"),
        Bucket(key="Amgen", label="Amgen", value=20, exactness="exact"),
    ]
    viz, _ = render(
        a_plan(group_by=GroupBy(dimension="lead_sponsor")),
        a_bucketset(buckets),
        ChartType.BAR_CHART,
        REGISTRY["lead_sponsor"],
        ctx,
    )

    assert [row["lead_sponsor"] for row in viz.data] == ["Amgen", "Merck", "Pfizer"]


async def test_overlapping_gets_a_coverage_annotation(
    settings: Settings, vocab: Vocabulary
) -> None:
    ctx = await a_ctx(settings, vocab)
    buckets = [Bucket(key="PHASE2", label="Phase 2", value=10, exactness="exact")]
    viz, _ = render(a_plan(), a_bucketset(buckets), ChartType.BAR_CHART, REGISTRY["phase"], ctx)

    assert viz.annotations is not None
    assert any("overlap" in a["text"].lower() for a in viz.annotations)


def test_title_drops_the_axis_qualifier_but_the_axis_keeps_it() -> None:
    """ "Trial phase" is right on an axis and wrong in a title; both readings are served."""
    from app.engine.dimensions import REGISTRY
    from app.render.encode import _dimension_noun

    assert _dimension_noun(REGISTRY["phase"]) == "Phase"
    assert REGISTRY["phase"].label == "Trial phase"
    # "Study type" keeps its qualifier: "Trials by Type" would say less, not more.
    assert _dimension_noun(REGISTRY["study_type"]) == "Study type"
    assert _dimension_noun(REGISTRY["country"]) == "Country"


async def test_axes_state_what_they_count(settings: Settings, vocab: Vocabulary) -> None:
    """ "1,750" is ambiguous between trials and people; the two metrics mean exactly those."""
    from app.models.plan import Metric

    ctx = await a_ctx(settings, vocab)
    buckets = [Bucket(key="PHASE2", label="Phase 2", value=1750, exactness="exact")]

    counted, _ = render(a_plan(), a_bucketset(buckets), ChartType.BAR_CHART, REGISTRY["phase"], ctx)
    enrolled, _ = render(
        a_plan(metric=Metric.ENROLLMENT_SUM),
        a_bucketset(buckets),
        ChartType.BAR_CHART,
        REGISTRY["phase"],
        ctx,
    )

    assert counted.encoding["y"]["unit"] == "studies"
    assert enrolled.encoding["y"]["unit"] == "participants"


async def test_an_empty_result_publishes_the_same_contract_as_a_populated_one(
    settings: Settings, vocab: Vocabulary
) -> None:
    """An empty result is not a different chart contract.

    The zero-row case collapses to `table` but used to carry an x/y encoding, so a client that
    switches on `type` and reads `encoding.columns` got nothing back for a table. The channels are
    a property of the chart type, not of whether any study matched.
    """
    ctx = await a_ctx(settings, vocab, max_buckets=20)
    plan = a_plan()
    dim = REGISTRY["phase"]

    populated, _ = render(
        plan,
        a_bucketset([Bucket(key="PHASE2", label="Phase 2", value=7.0, exactness="exact")]),
        ChartType.TABLE,
        dim,
        ctx,
    )
    empty, warnings = render(plan, a_bucketset([], total=0), ChartType.BAR_CHART, dim, ctx)

    assert empty.type is ChartType.TABLE
    assert empty.data == []
    assert any("empty visualization" in warning for warning in warnings)
    assert [column["field"] for column in empty.encoding["columns"]] == [
        column["field"] for column in populated.encoding["columns"]
    ]


async def test_an_empty_network_keeps_the_network_contract(
    settings: Settings, vocab: Vocabulary
) -> None:
    """The response model pins network encoding to exactly {nodes, edges}, populated or not."""
    ctx = await a_ctx(settings, vocab, max_buckets=20)

    empty, _ = render(
        a_plan(),
        a_bucketset([], total=0),
        ChartType.NETWORK_GRAPH,
        REGISTRY["intervention_name"],
        ctx,
    )

    assert empty.type is ChartType.NETWORK_GRAPH
    assert empty.data == {"nodes": [], "edges": []}
    assert set(empty.encoding) == {"nodes", "edges"}
    assert empty.encoding["edges"] == {"source": "source", "target": "target"}
