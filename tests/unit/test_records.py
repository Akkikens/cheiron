"""`complete_records` aggregation and the network graph. SPEC §5.2, §5.4; T10."""

from __future__ import annotations

import copy
from collections import Counter
from typing import Any

import httpx
import pytest

from app.analyze import analyze
from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary, VocabularyCache
from app.engine.bucketset import Bucket, BucketSet
from app.engine.context import RunContext, new_context
from app.engine.dimensions import REGISTRY
from app.engine.modes import network, records
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan, GroupBy, Intent, Metric, StudyFilter
from app.models.request import AnalyzeRequest, Options
from app.planner.base import PlanResult
from app.render.registry import select_chart
from tests.conftest import Handler, fixture_text, load_fixture, stub_transport
from tests.unit.test_engine_counts import DATA_TIMESTAMP, a1_upstream

STUDY = load_fixture("study_full.json")

FIXTURE_TOTAL = 2_400


_MISSING = object()


def _study(
    nct: str,
    *,
    phases: list[str] | object | None = _MISSING,
    start_date: str | None = "2020-06-15",
    enrollment: int | None = 40,
    sponsor: str = "Acme",
    interventions: list[str] | None = None,
    conditions: list[str] | None = None,
    empty_design: bool = False,
) -> dict[str, Any]:
    study = copy.deepcopy(STUDY)
    study["protocolSection"]["identificationModule"]["nctId"] = nct
    if empty_design:
        study["protocolSection"]["designModule"] = {}
        study["protocolSection"]["statusModule"] = {}
        return study

    design = study["protocolSection"]["designModule"]
    if phases is _MISSING:
        design["phases"] = ["PHASE2"]
    elif phases is None:
        design.pop("phases", None)
    else:
        design["phases"] = list(phases)  # type: ignore[arg-type]

    if enrollment is None:
        design.pop("enrollmentInfo", None)
    else:
        design["enrollmentInfo"] = {"count": enrollment, "type": "ACTUAL"}

    status = study["protocolSection"]["statusModule"]
    if start_date is None:
        status.pop("startDateStruct", None)
    else:
        status["startDateStruct"] = {"date": start_date, "type": "ACTUAL"}

    study["protocolSection"]["sponsorCollaboratorsModule"]["leadSponsor"] = {
        "name": sponsor,
        "class": "INDUSTRY",
    }
    study["protocolSection"]["armsInterventionsModule"]["interventions"] = [
        {"type": "DRUG", "name": name} for name in (interventions or ["DrugA"])
    ]
    study["protocolSection"]["conditionsModule"]["conditions"] = conditions or ["ConditionA"]
    return study


def phase_fixture(n: int = FIXTURE_TOTAL) -> list[dict[str, Any]]:
    """Studies whose phase distribution is known exactly for the agreement test."""
    studies: list[dict[str, Any]] = []
    for i in range(n):
        nct = f"NCT{i:08d}"
        if i < 100:
            studies.append(_study(nct, phases=["NA"]))
        elif i < 150:
            studies.append(_study(nct, phases=None))
        elif i < 650:
            studies.append(_study(nct, phases=["PHASE1"]))
        elif i < 1650:
            studies.append(_study(nct, phases=["PHASE2"]))
        elif i < 2150:
            studies.append(_study(nct, phases=["PHASE1", "PHASE2"]))
        else:
            studies.append(_study(nct, phases=["PHASE3"]))
    return studies


def expected_phase_counts(studies: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for study in studies:
        keys = records.membership_keys(study, REGISTRY["phase"])
        if keys is None:
            continue
        for key in keys:
            counts[key] += 1
    return dict(counts)


class PagingUpstream:
    """Serves a fixed study list with serial pageTokens bound to the parameter set."""

    def __init__(self, studies: list[dict[str, Any]], *, total: int | None = None) -> None:
        self.studies = studies
        self.total = total if total is not None else len(studies)
        self.requests: list[httpx.Request] = []
        self.version_reads = 0
        self._tokens: dict[str, int] = {}

    def handler(self) -> Handler:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)

            if request.url.path.endswith("/studies/enums"):
                return httpx.Response(200, text=fixture_text("studies_enums.json"))

            if request.url.path.endswith("/version"):
                self.version_reads += 1
                return httpx.Response(
                    200,
                    json={"apiVersion": "2.0.5", "dataTimestamp": DATA_TIMESTAMP},
                )

            if request.url.params.get("countTotal") == "true":
                return httpx.Response(200, json={"totalCount": self.total, "studies": []})

            page_size = int(request.url.params.get("pageSize", "10"))
            token = request.url.params.get("pageToken")
            offset = self._tokens[token] if token else 0
            chunk = self.studies[offset : offset + page_size]
            next_offset = offset + len(chunk)
            payload: dict[str, Any] = {"studies": chunk}
            if next_offset < len(self.studies):
                next_token = f"tok-{next_offset}"
                self._tokens[next_token] = next_offset
                payload["nextPageToken"] = next_token
            return httpx.Response(200, json=payload)

        return handle

    async def async_handler(self, request: httpx.Request) -> httpx.Response:
        return self.handler()(request)


async def records_context(
    settings: Settings,
    upstream: PagingUpstream,
    *,
    options: Options | None = None,
    budget: int = 40,
) -> RunContext:
    transport = stub_transport(settings, upstream.async_handler)
    client = CTGClient(transport)
    vocab = await Vocabulary.load(client)
    ctx = new_context(
        client,
        vocab,
        options or Options(include_citations=False),
        settings=settings.model_copy(update={"max_upstream_requests": budget}),
        data_timestamp=DATA_TIMESTAMP,
    )
    upstream.version_reads = 0
    return ctx


def distribution_plan(**overrides: Any) -> AnalysisPlan:
    base: dict[str, Any] = {
        "intent": Intent.DISTRIBUTION,
        "filters": StudyFilter(intervention="pembrolizumab"),
        "group_by": GroupBy(dimension="phase"),
        "metric": Metric.STUDY_COUNT,
        "interpretation": "Distribution of clinical trials studying pembrolizumab across phases.",
    }
    return AnalysisPlan(**{**base, **overrides})


# --- paging --------------------------------------------------------------------------------


async def test_serial_paging_repeats_every_non_paging_param(settings: Settings) -> None:
    studies = phase_fixture(FIXTURE_TOTAL)
    upstream = PagingUpstream(studies)
    ctx = await records_context(settings, upstream)

    fetched = await records.fetch_all(ctx, {"query.intr": "pembrolizumab"})

    assert len(fetched) == FIXTURE_TOTAL
    page_requests = [
        r
        for r in upstream.requests
        if r.url.path.endswith("/studies") and r.url.params.get("countTotal") != "true"
    ]
    # 1000 + 1000 + 400: the third page is the remainder, not an empty trailing call.
    assert len(page_requests) == 3

    def material(request: httpx.Request) -> dict[str, str]:
        return {
            key: value
            for key, value in request.url.params.items()
            if key not in {"countTotal", "pageSize", "pageToken"}
        }

    assert material(page_requests[0]) == material(page_requests[1]) == material(page_requests[2])
    assert page_requests[0].url.params.get("pageToken") is None
    assert page_requests[1].url.params.get("pageToken")
    assert page_requests[2].url.params.get("pageToken")


async def test_mismatched_page_token_raises(settings: Settings) -> None:
    studies = phase_fixture(1_100)
    upstream = PagingUpstream(studies)
    ctx = await records_context(settings, upstream)

    page1 = await ctx.client.page(
        {"query.intr": "pembrolizumab", "pageSize": "1000", "fields": "NCTId"},
        page_token=None,
    )
    assert page1.next_page_token is not None

    with pytest.raises(ValueError, match="different parameter set"):
        await ctx.client.page(
            {"query.intr": "other", "pageSize": "1000", "fields": "NCTId"},
            page_token=page1.next_page_token,
        )


# --- aggregation agreement -----------------------------------------------------------------


async def test_record_aggregation_agrees_with_independent_membership(
    settings: Settings,
) -> None:
    """Two independent implementations agreeing is the load-bearing T10 check."""
    studies = phase_fixture(FIXTURE_TOTAL)
    upstream = PagingUpstream(studies)
    ctx = await records_context(settings, upstream)
    plan = distribution_plan()
    dim = REGISTRY["phase"]

    bucketset, _ = await records.run(
        plan, dim, ctx, params={"query.intr": "pembrolizumab"}, total=FIXTURE_TOTAL
    )

    expected = expected_phase_counts(studies)
    assert {bucket.key: int(bucket.value) for bucket in bucketset.buckets} == expected
    assert bucketset.unclassified == 50
    assert bucketset.total == FIXTURE_TOTAL
    assert bucketset.mode == "complete_records"
    assert bucketset.semantics == "overlapping"


# --- data-quality traps --------------------------------------------------------------------


def test_explicit_na_and_absent_phases_are_distinct() -> None:
    na = _study("NCT00000001", phases=["NA"])
    missing = _study("NCT00000002", phases=None)

    assert records.membership_keys(na, REGISTRY["phase"]) == ["NA"]
    assert records.membership_keys(missing, REGISTRY["phase"]) is None


def test_mixed_date_precision_and_absent_date() -> None:
    dim = REGISTRY["start_year"]
    assert records.membership_keys(_study("a", start_date="2019-06"), dim) == ["2019"]
    assert records.membership_keys(_study("b", start_date="2019-06-15"), dim) == ["2019"]
    assert records.membership_keys(_study("c", start_date=None), dim) is None


def test_empty_modules_land_in_unclassified() -> None:
    study = _study("NCT00000102", empty_design=True)
    assert records.membership_keys(study, REGISTRY["phase"]) is None
    assert records.membership_keys(study, REGISTRY["overall_status"]) is None


async def test_winsorize_clamps_placeholders(settings: Settings) -> None:
    studies = [_study(f"NCT{i:08d}", enrollment=100 + i, phases=["PHASE2"]) for i in range(98)]
    studies.append(_study("NCT00000098", enrollment=99_999_999, phases=["PHASE2"]))
    studies.append(_study("NCT00000099", enrollment=188_814_085, phases=["PHASE2"]))

    upstream = PagingUpstream(studies)
    ctx = await records_context(settings, upstream)
    plan = distribution_plan(metric=Metric.ENROLLMENT_MEDIAN)

    bucketset = records.aggregate(studies, plan, REGISTRY["phase"], ctx)

    assert any("winsorized" in a.lower() for a in ctx.assumptions)
    assert any("clamped" in a for a in ctx.assumptions)
    median = next(b.value for b in bucketset.buckets if b.key == "PHASE2")
    assert median < 10_000


def test_enrollment_unplannable_above_threshold() -> None:
    error = records.enrollment_unplannable(25_000, 2_000)
    assert isinstance(error, CheironError)
    assert error.code is ErrorCode.UNPLANNABLE_QUERY
    assert "25,000" in error.message
    assert "2,000" in error.message


# --- citations free ------------------------------------------------------------------------


async def test_citations_add_zero_extra_requests(settings: Settings) -> None:
    studies = [_study(f"NCT{i:08d}", phases=["PHASE2"]) for i in range(50)]
    upstream = PagingUpstream(studies)
    ctx = await records_context(
        settings, upstream, options=Options(include_citations=True, citations_per_datum=2)
    )
    plan = distribution_plan()

    before = len(
        [
            r
            for r in upstream.requests
            if r.url.path.endswith("/studies") and r.url.params.get("countTotal") != "true"
        ]
    )
    bucketset, _ = await records.run(
        plan, REGISTRY["phase"], ctx, params={"query.intr": "x"}, total=50
    )
    after = [
        r
        for r in upstream.requests
        if r.url.path.endswith("/studies") and r.url.params.get("countTotal") != "true"
    ]

    assert len(after) - before == 1
    phase2 = next(b for b in bucketset.buckets if b.key == "PHASE2")
    assert len(phase2.citations) == 2
    assert phase2.citations[0].field == REGISTRY["phase"].record_path


# --- network -------------------------------------------------------------------------------


async def test_network_exact_edge_weights(settings: Settings) -> None:
    studies = [
        _study("NCT1", sponsor="Merck", interventions=["Pembrolizumab", "Chemo"]),
        _study("NCT2", sponsor="Merck", interventions=["Pembrolizumab", "Chemo"]),
        _study("NCT3", sponsor="Pfizer", interventions=["Pembrolizumab"]),
        _study("NCT4", sponsor="Pfizer", interventions=["Aspirin"]),
    ]
    upstream = PagingUpstream(studies)
    ctx = await records_context(settings, upstream)
    plan = distribution_plan(
        intent=Intent.NETWORK,
        filters=StudyFilter(sponsor="Merck"),
        interpretation="Co-occurrence network of sponsors and interventions.",
    )

    viz, _ = network.build(studies, plan, ctx, pairing="sponsor_intervention")

    assert viz.type.value == "network_graph"
    edges = {(e["source"], e["target"]): e["weight"] for e in viz.data["edges"]}
    assert edges[("Chemo", "Merck")] == 2
    assert edges[("Merck", "Pembrolizumab")] == 2
    assert ("Aspirin", "Pfizer") not in edges
    assert ("Pembrolizumab", "Pfizer") not in edges

    annotation = viz.annotations[0]
    assert "showing" in annotation["text"]
    assert "nodes" in annotation["text"]
    assert "edges" in annotation["text"]


def test_a7_network_outside_records_downgrades() -> None:
    bucketset = BucketSet(
        buckets=[Bucket(key="PHASE2", label="Phase 2", value=100.0, exactness="exact")],
        total=25_000,
        unclassified=0,
        semantics="overlapping",
        mode="server_counts",
    )
    plan = distribution_plan(intent=Intent.NETWORK)
    chosen, warnings = select_chart(plan, bucketset, REGISTRY["phase"], Options())

    assert chosen.value == "grouped_bar_chart"
    assert any("25,000" in w for w in warnings)
    assert any("complete_records" in w for w in warnings)


async def test_analyze_uses_complete_records_below_threshold(settings: Settings) -> None:
    studies = [_study(f"NCT{i:08d}", phases=["PHASE2"]) for i in range(10)]
    upstream = PagingUpstream(studies, total=10)
    transport = stub_transport(settings, upstream.async_handler)
    cache = VocabularyCache(ttl_seconds=3600)

    response = await analyze(
        AnalyzeRequest(query="How many trials by phase?", drug_name="Pembrolizumab"),
        transport=transport,
        vocabulary_cache=cache,
        settings=settings,
    )

    assert response.meta.coverage.aggregation_mode == "complete_records"
    assert response.meta.total_matching_studies == 10
    # Single phase bucket → KPI; the mode wire is what this test pins.
    assert response.visualization.type.value in {"kpi", "bar_chart"}
    assert len(response.visualization.data) == 1


async def test_enrollment_above_threshold_refuses(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = a1_upstream()
    transport = stub_transport(settings, upstream.async_handler)
    cache = VocabularyCache(ttl_seconds=3600)

    async def fake_plan(
        request: AnalyzeRequest, vocab: Vocabulary, **_kwargs: object
    ) -> PlanResult:
        return PlanResult(
            plan=distribution_plan(metric=Metric.ENROLLMENT_SUM),
            planner="heuristic_fallback",
            attempts=1,
        )

    import app.analyze as analyze_mod

    monkeypatch.setattr(analyze_mod, "_plan", fake_plan)

    with pytest.raises(CheironError) as excinfo:
        await analyze(
            AnalyzeRequest(query="enrollment by phase", drug_name="Pembrolizumab"),
            transport=transport,
            vocabulary_cache=cache,
            settings=settings,
        )

    assert excinfo.value.code is ErrorCode.UNPLANNABLE_QUERY
    assert "enrollment" in excinfo.value.message.lower()


async def test_record_mode_narrows_without_ever_claiming_it_capped(settings: Settings) -> None:
    """Every category was counted in memory, so dropped ones are known exactly, not unknowable."""
    studies = [
        _study(f"NCT{i:08d}", sponsor=f"Sponsor {i % 5}", phases=["PHASE2"]) for i in range(20)
    ]
    upstream = PagingUpstream(studies)
    ctx = await records_context(
        settings, upstream, options=Options(max_buckets=2, include_citations=False)
    )
    plan = distribution_plan(group_by=GroupBy(dimension="lead_sponsor"))

    bucketset = records.aggregate(studies, plan, REGISTRY["lead_sponsor"], ctx)

    assert len(bucketset.buckets) == 2
    assert bucketset.aggregation_capped is False
    assert bucketset.omitted_buckets == 3
    # The three omitted sponsors carry 4 studies each, and nothing is lost from the arithmetic.
    assert bucketset.omitted_value == 12.0
    assert bucketset.bucket_sum + int(bucketset.omitted_value) == 20


async def test_two_casings_of_one_intervention_are_one_bucket(settings: Settings) -> None:
    """Record mode split them only cosmetically, but the split was still wrong twice over.

    One drug drew as two bars, and the same question returned different labels either side of the
    2,000-study threshold, where the sampled path folds them because upstream matching is
    case-insensitive.
    """
    studies = [_study(f"NCT{i:08d}", interventions=["Placebo"]) for i in range(6)] + [
        _study(f"NCT1{i:07d}", interventions=["placebo"]) for i in range(4)
    ]
    upstream = PagingUpstream(studies)
    ctx = await records_context(settings, upstream)
    plan = distribution_plan(group_by=GroupBy(dimension="intervention_name"))

    bucketset = records.aggregate(studies, plan, REGISTRY["intervention_name"], ctx)

    assert [bucket.key for bucket in bucketset.buckets] == ["Placebo"]
    assert bucketset.buckets[0].value == 10.0
    assert any("capitalisation" in assumption for assumption in ctx.assumptions)


async def test_a_study_listing_both_casings_is_counted_once(settings: Settings) -> None:
    """Folding must not rebuild the double count inside a single bucket.

    A trial can list "Placebo" and "placebo" as two separate arms. Folding the keys without
    deduplicating them per study would credit that one trial twice to the merged bucket, which is
    the exact arithmetic the fold exists to remove.
    """
    studies = [_study("NCT00000001", interventions=["Placebo", "placebo"])]
    upstream = PagingUpstream(studies)
    ctx = await records_context(settings, upstream)
    plan = distribution_plan(group_by=GroupBy(dimension="intervention_name"))

    bucketset = records.aggregate(studies, plan, REGISTRY["intervention_name"], ctx)

    assert [bucket.key for bucket in bucketset.buckets] == ["Placebo"]
    assert bucketset.buckets[0].value == 1.0


async def test_the_network_draws_one_node_per_treatment_not_one_per_casing(
    settings: Settings,
) -> None:
    """What a reviewer sees first: `topiramate` and `Topiramate` as two dots on one graph."""
    studies = [
        _study(f"NCT{i:08d}", conditions=["Migraine"], interventions=["Topiramate"])
        for i in range(5)
    ] + [
        _study(f"NCT1{i:07d}", conditions=["Migraine"], interventions=["topiramate"])
        for i in range(4)
    ]
    upstream = PagingUpstream(studies)
    ctx = await records_context(settings, upstream)
    plan = distribution_plan(
        intent=Intent.NETWORK,
        filters=StudyFilter(condition="Migraine"),
        group_by=GroupBy(dimension="intervention_name"),
    )

    viz, _ = network.build(studies, plan, ctx)

    labels = [node["label"] for node in viz.data["nodes"]]
    assert sorted(labels) == ["Migraine", "Topiramate"]
    edge = next(e for e in viz.data["edges"])
    assert edge["weight"] == 9, "both casings contribute to the one edge"
