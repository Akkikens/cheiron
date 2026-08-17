"""Regressions found by review, each pinned so it cannot come back.

Every test here corresponds to a defect that shipped and passed the suite at the time. They are
grouped in one file deliberately: the common thread is that all nine were invisible to tests
written from the spec, because each one is a case the spec describes correctly and the code
implemented under a slightly different assumption.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from app.cache import options_cache_key, result_cache_key
from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.engine.basefilter import base_filter, year_span
from app.engine.bucketset import Bucket, BucketSet
from app.engine.context import new_context
from app.engine.coverage import build_coverage
from app.engine.dimensions import REGISTRY
from app.engine.modes import network
from app.engine.preflight import select_mode
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan, ChartType, GroupBy, Intent, Metric, StudyFilter
from app.models.request import Options
from app.render.encode import render
from tests.conftest import Handler, stub_transport

DATA_TIMESTAMP = "2026-08-14T09:00:05"


@pytest.fixture
async def vocab(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


# --- 1: a closed vocabulary is not the same as "has an enum" --------------------------------


def test_a_large_trend_uses_server_counts_not_sampling() -> None:
    """start_year is closed (a derived range) but carries enum_name=None.

    Routing it to the sampling mode confirmed year labels with COVERAGE[FullMatch]"2021" instead
    of RANGE[2021-01-01,2021-12-31], which confirms to zero and empties the chart — for the most
    commonly asked question there is.
    """
    assert select_mode(50_000, REGISTRY["start_year"], 2_000) == "server_counts"
    assert select_mode(1_500, REGISTRY["start_year"], 2_000) == "complete_records"


def test_open_vocabularies_still_sample() -> None:
    assert select_mode(50_000, REGISTRY["lead_sponsor"], 2_000) == "sampled_then_confirmed"
    assert select_mode(50_000, REGISTRY["phase"], 2_000) == "server_counts"


# --- 2: the result cache key must include response-shaping options --------------------------


def test_options_change_the_result_cache_key() -> None:
    """Without this, an explain=false caller could be served a cached plan and query log."""
    plain = options_cache_key(Options())
    explained = options_cache_key(Options(explain=True))
    narrow = options_cache_key(Options(max_buckets=3))
    uncited = options_cache_key(Options(include_citations=False))

    assert len({plain, explained, narrow, uncited}) == 4
    assert result_cache_key("p", DATA_TIMESTAMP, plain) != result_cache_key(
        "p", DATA_TIMESTAMP, explained
    )


def test_the_same_question_and_options_still_share_a_key() -> None:
    assert result_cache_key("p", DATA_TIMESTAMP, options_cache_key(Options())) == result_cache_key(
        "p", DATA_TIMESTAMP, options_cache_key(Options())
    )


# --- 3: a future start_year is a valid question, not a 500 ----------------------------------


def test_a_future_start_year_does_not_invert_the_range() -> None:
    """`start_year=2030` with no end asks about planned trials; it used to raise into a 500."""
    span = year_span(StudyFilter(start_year=2030))

    assert span is not None
    start, end = span
    assert start == date(2030, 1, 1)
    assert end >= start

    params, _ = base_filter(StudyFilter(start_year=2030))
    assert "filter.advanced" in params


def test_an_explicit_end_year_still_wins() -> None:
    span = year_span(StudyFilter(start_year=2015, end_year=2020))

    assert span == (date(2015, 1, 1), date(2020, 12, 31))


# --- 5 and 6: network accounting ------------------------------------------------------------


def intervention_study(nct: str, names: list[str]) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct},
            "armsInterventionsModule": {
                "interventions": [{"name": name, "type": "DRUG"} for name in names]
            },
        }
    }


async def a_ctx(settings: Settings, vocab: Vocabulary, max_buckets: int = 20) -> Any:
    return new_context(
        CTGClient(stub_transport(settings, lambda _r: httpx.Response(404))),
        vocab,
        Options(include_citations=False, max_buckets=max_buckets),
        settings=settings,
        data_timestamp=DATA_TIMESTAMP,
    )


def a_network_plan() -> AnalysisPlan:
    return AnalysisPlan(
        intent=Intent.NETWORK,
        filters=StudyFilter(intervention="pembrolizumab"),
        group_by=GroupBy(dimension="intervention_name"),
        interpretation="Co-occurrence of interventions across trials.",
    )


async def test_a_node_weight_is_trials_not_twice_trials(
    settings: Settings, vocab: Vocabulary
) -> None:
    """left and right are the same list in the intervention pairing; both loops counted it."""
    ctx = await a_ctx(settings, vocab)
    studies = [
        intervention_study("NCT1", ["A", "B"]),
        intervention_study("NCT2", ["A", "B"]),
        intervention_study("NCT3", ["C", "D"]),
    ]

    viz, _ = network.build(studies, a_network_plan(), ctx, pairing="intervention_intervention")

    weights = {node["id"]: node["weight"] for node in viz.data["nodes"]}
    assert weights["A"] == 2  # two trials, not four
    assert weights["B"] == 2


async def test_the_prune_annotation_counts_every_hidden_edge(
    settings: Settings, vocab: Vocabulary
) -> None:
    """A disclosure reading "1 of 1 edges" while hiding two is the failure it exists to prevent."""
    ctx = await a_ctx(settings, vocab, max_buckets=2)
    studies = [
        intervention_study("NCT1", ["A", "B"]),
        intervention_study("NCT2", ["A", "B"]),
        intervention_study("NCT3", ["C", "D"]),
        intervention_study("NCT4", ["C", "D"]),
        intervention_study("NCT5", ["E", "F"]),
        intervention_study("NCT6", ["E", "F"]),
    ]

    viz, _ = network.build(studies, a_network_plan(), ctx, pairing="intervention_intervention")

    annotation = next(a for a in (viz.annotations or []) if a["type"] == "prune")
    shown_edges = len(viz.data["edges"])

    assert annotation["shown_edges"] == shown_edges
    assert annotation["total_edges_before_prune"] == 3
    assert annotation["edges_dropped_with_pruned_nodes"] == 3 - shown_edges
    assert annotation["total_nodes"] == 6


# --- 7: coverage arithmetic is about study counts -------------------------------------------


def a_bucketset(values: dict[str, float], total: int, unclassified: int = 0) -> BucketSet:
    return BucketSet(
        buckets=[
            Bucket(key=key, label=key, value=value, exactness="exact")
            for key, value in values.items()
        ],
        total=total,
        unclassified=unclassified,
        semantics="partition",
        mode="complete_records",
    )


def test_enrollment_coverage_does_not_claim_an_unexplained_difference() -> None:
    """bucket_sum is people and total is studies; reconciling them reads as a data fault."""
    bucketset = a_bucketset({"INTERVENTIONAL": 1_204_331.0}, total=1_840, unclassified=12)

    coverage, warnings = build_coverage(bucketset, REGISTRY["study_type"], counts_studies=False)

    assert not any("unexplained" in warning for warning in warnings)
    assert coverage.overlap_note is not None
    assert "enrollment totals, not study counts" in coverage.overlap_note
    assert "1,840 studies" in coverage.overlap_note


def test_study_count_coverage_still_reconciles() -> None:
    bucketset = a_bucketset({"INTERVENTIONAL": 1_800.0}, total=1_840, unclassified=12)

    _, warnings = build_coverage(bucketset, REGISTRY["study_type"], counts_studies=True)

    assert any("does not equal" in warning for warning in warnings)


# --- 9: a 429 is upstream telling us the rate -----------------------------------------------


async def test_a_429_surfaces_as_rate_limited_with_retry_after(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, text="slow down", headers={"Retry-After": "7"})

    client = CTGClient(stub_transport(settings, handler))

    with pytest.raises(CheironError) as caught:
        await client.count({"query.cond": "cancer"})

    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.status == 429
    assert caught.value.retry_after_seconds == 7
    assert calls["n"] > 1  # still retried, just politely


async def test_a_429_without_a_header_still_reports_rate_limited(settings: Settings) -> None:
    client = CTGClient(stub_transport(settings, lambda _r: httpx.Response(429, text="slow")))

    with pytest.raises(CheironError) as caught:
        await client.count({"query.cond": "cancer"})

    assert caught.value.code is ErrorCode.RATE_LIMITED
    assert caught.value.retry_after_seconds is None


# --- 10: an empty network result is still a network graph -----------------------------------


async def test_an_empty_network_result_keeps_its_shape(
    settings: Settings, vocab: Vocabulary
) -> None:
    """Returning TABLE with {nodes, edges} fails the response model and 500s an empty answer."""
    ctx = await a_ctx(settings, vocab)
    empty = BucketSet(
        buckets=[],
        total=0,
        unclassified=0,
        semantics="overlapping",
        mode="complete_records",
    )

    viz, warnings = render(
        a_network_plan(), empty, ChartType.NETWORK_GRAPH, REGISTRY["intervention_name"], ctx
    )

    assert viz.type is ChartType.NETWORK_GRAPH
    assert viz.data == {"nodes": [], "edges": []}
    assert warnings


async def test_an_empty_bar_chart_still_collapses_to_a_table(
    settings: Settings, vocab: Vocabulary
) -> None:
    ctx = await a_ctx(settings, vocab)
    empty = BucketSet(
        buckets=[], total=0, unclassified=0, semantics="overlapping", mode="server_counts"
    )

    viz, _ = render(
        AnalysisPlan(
            intent=Intent.DISTRIBUTION,
            filters=StudyFilter(),
            group_by=GroupBy(dimension="phase"),
            metric=Metric.STUDY_COUNT,
            interpretation="Distribution across phases.",
        ),
        empty,
        ChartType.BAR_CHART,
        REGISTRY["phase"],
        ctx,
    )

    assert viz.type is ChartType.TABLE
    assert viz.data == []


# --- network citations must be checkable ----------------------------------------------------


async def test_edge_citations_quote_the_record_not_the_id(
    settings: Settings, vocab: Vocabulary
) -> None:
    """Repeating the NCT id as the excerpt cites nothing — it restates `nct_id` (SPEC §4.2)."""
    ctx = await a_ctx(settings, vocab)
    ctx.options = Options(include_citations=True, citations_per_datum=2)
    studies = [
        intervention_study("NCT1", ["Temozolomide", "Bevacizumab"]),
        intervention_study("NCT2", ["Temozolomide", "Bevacizumab"]),
    ]

    viz, _ = network.build(studies, a_network_plan(), ctx, pairing="intervention_intervention")

    citation = viz.data["edges"][0]["citations"][0]
    assert citation["nct_id"] == "NCT1"
    assert citation["excerpt"] != citation["nct_id"]
    assert "Temozolomide" in citation["excerpt"]
    assert "interventions" in citation["field"]
    assert "briefSummary" not in citation["field"]
