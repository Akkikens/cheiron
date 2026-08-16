"""Visualization encoding: Other rollup, titles, sort order. SPEC §4.1, §6.2."""

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
    assert viz.title == "Pembrolizumab Trials by Trial phase"
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


async def test_other_rollup_names_count_and_sum(settings: Settings, vocab: Vocabulary) -> None:
    """The one place T07 can silently drop data — the annotation has to name both."""
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

    assert len(viz.data) == 20
    other = viz.data[-1]
    assert other["lead_sponsor"] == "OTHER"
    assert other["lead_sponsor_label"].startswith("Other (31 categories)")

    # Top 19 by value are 100..82 (i=0..18); rolled are i=19..49 with values 81..51.
    rolled_sum = sum(range(51, 82))
    assert other["study_count"] == rolled_sum
    assert viz.annotations is not None
    note = next(a for a in viz.annotations if a["type"] == "rollup")
    assert note["rolled_categories"] == 31
    assert note["rolled_value"] == rolled_sum
    assert "31" in note["text"]
    assert f"{rolled_sum:,}" in note["text"]


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
