"""The `server_counts` fan-out, preflight, and coverage math. SPEC §5.2, §5.3, §4.3.

The A1 numbers below are live measurements, re-verified 2026-08-16 against
`query.intr=pembrolizumab`. They are pinned here rather than tolerated, because the point of A1
is that the buckets reconcile to a stated arithmetic — a test that accepts drift cannot tell a
data update from a broken predicate.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.engine.basefilter import SPONSOR_ASSUMPTION, base_filter, with_predicate
from app.engine.bucketset import Bucket, BucketSet
from app.engine.context import (
    BudgetExhausted,
    DataTimestampChanged,
    RunContext,
    budget_error,
    new_context,
)
from app.engine.coverage import build_coverage
from app.engine.dimensions import REGISTRY
from app.engine.modes import counts
from app.engine.preflight import preflight, select_mode, unimplemented_mode
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan, Bin, GroupBy, Intent, Metric, StudyFilter
from app.models.request import Options
from tests.conftest import Handler, fixture_text, stub_transport

# SPEC A1, measured live: query.intr=pembrolizumab grouped by phase.
A1_TOTAL = 2_927
A1_MISSING = 169
A1_BUCKETS = {
    "NA": 53,
    "EARLY_PHASE1": 51,
    "PHASE1": 1_039,
    "PHASE2": 1_750,
    "PHASE3": 363,
    "PHASE4": 17,
}
A1_SUM = 3_273
A1_WITH_VALUE = A1_TOTAL - A1_MISSING  # 2,758
A1_OVERLAP = A1_SUM - A1_WITH_VALUE  # 515

DATA_TIMESTAMP = "2026-08-14T09:00:05"


def a1_plan() -> AnalysisPlan:
    return AnalysisPlan(
        intent=Intent.DISTRIBUTION,
        filters=StudyFilter(intervention="pembrolizumab"),
        group_by=GroupBy(dimension="phase"),
        metric=Metric.STUDY_COUNT,
        interpretation="Distribution of clinical trials studying pembrolizumab across phases.",
    )


class Upstream:
    """A `/studies` stub that answers counts from a predicate -> count mapping.

    Refuses any predicate it was not told about, so a builder change that alters an expression
    surfaces as a failure here instead of silently scoring zero.
    """

    def __init__(
        self,
        counts_by_predicate: dict[str | None, int],
        *,
        data_timestamps: list[str] | None = None,
        fail_predicates: dict[str, Exception] | None = None,
        fail_citation_predicates: dict[str, Exception] | None = None,
        studies_by_predicate: dict[str, list[dict[str, Any]]] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.counts_by_predicate = counts_by_predicate
        self.data_timestamps = data_timestamps or [DATA_TIMESTAMP]
        self.fail_predicates = fail_predicates or {}
        self.fail_citation_predicates = fail_citation_predicates or {}
        self.studies_by_predicate = studies_by_predicate or {}
        self.delay = delay
        self.requests: list[httpx.Request] = []
        self.version_reads = 0
        self.concurrent = 0
        self.peak_concurrent = 0

    def handler(self) -> Handler:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)

            if request.url.path.endswith("/studies/enums"):
                return httpx.Response(200, text=fixture_text("studies_enums.json"))

            if request.url.path.endswith("/version"):
                index = min(self.version_reads, len(self.data_timestamps) - 1)
                self.version_reads += 1
                return httpx.Response(
                    200,
                    json={
                        "apiVersion": "2.0.5",
                        "dataTimestamp": self.data_timestamps[index],
                    },
                )

            predicate = request.url.params.get("filter.advanced")
            is_count = request.url.params.get("countTotal") == "true"

            if is_count and predicate in self.fail_predicates:
                raise self.fail_predicates[predicate]
            if not is_count and predicate in self.fail_citation_predicates:
                raise self.fail_citation_predicates[predicate]

            if predicate not in self.counts_by_predicate:
                return httpx.Response(400, text=f"unstubbed predicate: {predicate}")

            if is_count:
                return httpx.Response(
                    200, json={"totalCount": self.counts_by_predicate[predicate], "studies": []}
                )

            page_size = int(request.url.params.get("pageSize", "10"))
            studies = list(self.studies_by_predicate.get(predicate or "", []))[:page_size]
            return httpx.Response(
                200,
                json={
                    "totalCount": self.counts_by_predicate[predicate],
                    "studies": studies,
                },
            )

        return handle

    async def async_handler(self, request: httpx.Request) -> httpx.Response:
        """Yields, so concurrent fan-out genuinely overlaps rather than running serially."""
        self.concurrent += 1
        self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        try:
            await asyncio.sleep(self.delay)
            return self.handler()(request)
        finally:
            self.concurrent -= 1


def a1_upstream(**kwargs: Any) -> Upstream:
    predicates = {f"AREA[Phase]{value}": count for value, count in A1_BUCKETS.items()} | {
        "AREA[Phase]MISSING": A1_MISSING,
        None: A1_TOTAL,
    }
    return Upstream(predicates, **kwargs)


async def a_context(
    settings: Settings, upstream: Upstream, *, options: Options | None = None, budget: int = 40
) -> RunContext:
    transport = stub_transport(settings, upstream.async_handler)
    client = CTGClient(transport)
    vocab = await Vocabulary.load(client)
    # Citation tests opt in explicitly; the count-fan-out suite stays citation-free so its
    # spend and concurrency assertions stay about counts.
    ctx = new_context(
        client,
        vocab,
        options or Options(include_citations=False),
        settings=settings.model_copy(update={"max_upstream_requests": budget}),
        data_timestamp=DATA_TIMESTAMP,
    )
    upstream.version_reads = 0
    return ctx


# --- base filter: the parameter split is load-bearing --------------------------------------


def test_drug_name_goes_to_query_intr_and_never_to_filter_advanced() -> None:
    """2,927 under query.intr vs 2,531 under filter.advanced (notes §2)."""
    params, _ = base_filter(StudyFilter(intervention="pembrolizumab"))

    assert params["query.intr"] == "pembrolizumab"
    assert "filter.advanced" not in params


def test_free_text_filters_use_their_documented_query_params() -> None:
    params, _ = base_filter(
        StudyFilter(
            intervention="Pembrolizumab", condition="Melanoma", sponsor="Merck", term="immune"
        )
    )

    assert params == {
        "query.intr": "Pembrolizumab",
        "query.cond": "Melanoma",
        "query.lead": "Merck",
        "query.term": "immune",
    }


def test_sponsor_use_is_disclosed() -> None:
    """SPEC §2.1: query.lead vs query.spons differ materially, so the choice is stated."""
    _, assumptions = base_filter(StudyFilter(sponsor="Pfizer"))

    assert len(assumptions) == 1
    assert "query.lead" in assumptions[0]
    assert SPONSOR_ASSUMPTION in assumptions


def test_no_sponsor_means_no_assumption() -> None:
    _, assumptions = base_filter(StudyFilter(intervention="Pembrolizumab"))

    assert assumptions == []


def test_structured_filters_go_to_filter_advanced() -> None:
    params, _ = base_filter(StudyFilter(phase=["PHASE2", "PHASE3"], study_type="INTERVENTIONAL"))

    advanced = params["filter.advanced"]
    assert "AREA[Phase]PHASE2" in advanced
    assert "AREA[Phase]PHASE3" in advanced
    assert "AREA[StudyType]INTERVENTIONAL" in advanced
    assert " OR " in advanced


def test_country_is_an_area_predicate_not_a_query_param() -> None:
    """SPEC §2.1 maps country to AREA[LocationCountry], unlike the other free-text fields."""
    params, _ = base_filter(StudyFilter(country="France"))

    assert "query.locn" not in params
    assert params["filter.advanced"] == 'AREA[LocationCountry]COVERAGE[FullMatch]"France"'


def test_year_bounds_become_a_date_range() -> None:
    params, _ = base_filter(StudyFilter(start_year=2020, end_year=2021))

    assert params["filter.advanced"] == "AREA[StartDate]RANGE[2020-01-01,2021-12-31]"


def test_an_open_ended_year_range_does_not_use_max() -> None:
    """MAX would include the 2099 garbage notes §6.3 found at the top of the corpus."""
    params, _ = base_filter(StudyFilter(start_year=2020))

    assert "MAX" not in params["filter.advanced"]
    assert params["filter.advanced"].startswith("AREA[StartDate]RANGE[2020-01-01,")


def test_bucket_predicates_are_anded_not_concatenated() -> None:
    """String concatenation would let the base filter's OR re-associate and widen the bucket."""
    params, _ = base_filter(StudyFilter(phase=["PHASE2", "PHASE3"]))

    merged = with_predicate(params, "AREA[OverallStatus]RECRUITING")

    assert merged["filter.advanced"] == (
        "(((AREA[Phase]PHASE2) OR (AREA[Phase]PHASE3)) AND (AREA[OverallStatus]RECRUITING))"
    )


def test_with_predicate_leaves_the_base_filter_untouched() -> None:
    params, _ = base_filter(StudyFilter(intervention="pembrolizumab"))

    with_predicate(params, "AREA[Phase]PHASE2")

    assert "filter.advanced" not in params


# --- preflight -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "dimension", "expected"),
    [
        (1_999, "phase", "complete_records"),
        (2_000, "phase", "complete_records"),
        (2_001, "phase", "server_counts"),
        (1_999, "lead_sponsor", "complete_records"),
        (2_000, "lead_sponsor", "complete_records"),
        (2_001, "lead_sponsor", "sampled_then_confirmed"),
    ],
)
def test_mode_selection_at_the_threshold(total: int, dimension: str, expected: str) -> None:
    assert select_mode(total, REGISTRY[dimension], 2_000) == expected


async def test_preflight_issues_exactly_one_count(settings: Settings) -> None:
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)

    result = await preflight(a1_plan(), REGISTRY["phase"], ctx, threshold=2_000)

    studies_requests = [r for r in upstream.requests if r.url.path.endswith("/studies")]
    assert len(studies_requests) == 1
    assert result.total == A1_TOTAL
    assert result.mode == "server_counts"
    assert ctx.spent == 1


async def test_preflight_url_carries_the_a1_shape(settings: Settings) -> None:
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)

    await preflight(a1_plan(), REGISTRY["phase"], ctx, threshold=2_000)

    url = next(r.url for r in upstream.requests if r.url.path.endswith("/studies"))
    query = urllib.parse.parse_qs(str(url.query.decode()))

    assert query["query.intr"] == ["pembrolizumab"]
    assert query["countTotal"] == ["true"]
    assert query["pageSize"] == ["1"]
    assert "filter.advanced" not in query


def test_unimplemented_modes_refuse_rather_than_downgrade() -> None:
    error = unimplemented_mode("sampled_then_confirmed", 51_610, REGISTRY["lead_sponsor"])

    assert error.code is ErrorCode.UNPLANNABLE_QUERY
    assert "51,610" in error.message
    assert "not implemented" in error.message
    assert error.details[0]["suggestion"]


# --- the fan-out ---------------------------------------------------------------------------


async def test_a1_reconciles(settings: Settings) -> None:
    """SPEC A1: the whole point of the engine, in one assertion block."""
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    assert {bucket.key: int(bucket.value) for bucket in bucketset.buckets} == A1_BUCKETS
    assert bucketset.bucket_sum == A1_SUM
    assert bucketset.unclassified == A1_MISSING
    assert bucketset.total == A1_TOTAL
    assert bucketset.semantics == "overlapping"
    assert all(bucket.exactness == "exact" for bucket in bucketset.buckets)


async def test_a1_coverage(settings: Settings) -> None:
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)
    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    coverage, warnings = build_coverage(bucketset, dim)

    assert coverage.groupby_semantics == "overlapping"
    assert coverage.bucket_sum == A1_SUM
    assert coverage.unclassified_count == A1_MISSING
    assert coverage.sample_size is None
    assert coverage.sample_coverage is None
    assert warnings == []


async def test_na_and_missing_are_distinct_buckets(settings: Settings) -> None:
    """notes §6.1: 234,433 explicit NA against 141,903 absent. Conflating them is a silent bug."""
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    na = next(bucket for bucket in bucketset.buckets if bucket.key == "NA")
    assert int(na.value) == 53
    assert bucketset.unclassified == 169
    assert "MISSING" not in {bucket.key for bucket in bucketset.buckets}

    predicates = {
        r.url.params.get("filter.advanced")
        for r in upstream.requests
        if r.url.path.endswith("/studies")
    }
    assert "AREA[Phase]NA" in predicates
    assert "AREA[Phase]MISSING" in predicates


async def test_bucket_values_come_from_the_live_enum_not_a_literal(settings: Settings) -> None:
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    keys = [bucket.key for bucket in bucketset.buckets]
    live = ctx.vocab.values("Phase")

    assert set(keys) == set(live), "every bucket is a live enum value, and none is invented"
    assert live[0] == "NA", "upstream declares NA first"
    assert keys[-1] == "NA", "SPEC §4 wants it last (notes §7)"
    assert [bucket.label for bucket in bucketset.buckets] == [
        "Early Phase 1",
        "Phase 1",
        "Phase 2",
        "Phase 3",
        "Phase 4",
        "Not Applicable",
    ]


async def test_the_synthetic_missing_bucket_is_never_a_query_value(settings: Settings) -> None:
    """`sort_order` is SPEC §4's display array and includes MISSING, which is not an enum value.

    Enumerating it as a bucket yields `AREA[Phase]\\MISSING` — the escaped literal word — which
    returns zero at HTTP 200 and double-counts the unclassified probe.
    """
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    assert "MISSING" in ctx.vocab.sort_order("Phase"), "the display order still carries it"
    assert "MISSING" not in {bucket.key for bucket in bucketset.buckets}

    predicates = {
        r.url.params.get("filter.advanced")
        for r in upstream.requests
        if r.url.path.endswith("/studies")
    }
    assert "AREA[Phase]MISSING" in predicates
    assert not any("\\MISSING" in (p or "") for p in predicates)


async def test_the_fan_out_is_concurrent(settings: Settings) -> None:
    upstream = a1_upstream(delay=0.01)
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    # Six enum buckets plus the MISSING probe; serial execution would peak at 1.
    assert upstream.peak_concurrent > 1


async def test_one_failed_bucket_fails_the_whole_group_by(settings: Settings) -> None:
    """SPEC §4.5: partial aggregations are never rendered. A missing bar reads as a finding."""
    upstream = a1_upstream(
        fail_predicates={"AREA[Phase]PHASE3": httpx.ConnectError("upstream went away")}
    )
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    with pytest.raises(CheironError) as caught:
        await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    assert caught.value.code is ErrorCode.UPSTREAM_ERROR


async def test_a_failed_bucket_leaves_no_pending_requests(settings: Settings) -> None:
    """The siblings are cancelled rather than left running with nobody awaiting them."""
    upstream = a1_upstream(
        delay=0.02, fail_predicates={"AREA[Phase]NA": httpx.ConnectError("boom")}
    )
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    with pytest.raises(CheironError):
        await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    await asyncio.sleep(0.05)
    assert upstream.concurrent == 0


# --- SPEC §7: the dataset must not move under us --------------------------------------------


async def test_a_changed_data_timestamp_aborts_the_group_by(settings: Settings) -> None:
    upstream = a1_upstream(data_timestamps=["2026-08-15T09:00:05"])
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    with pytest.raises(DataTimestampChanged) as caught:
        await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    assert caught.value.captured == DATA_TIMESTAMP
    assert caught.value.observed == "2026-08-15T09:00:05"
    assert "2026-08-15T09:00:05" in str(caught.value)


async def test_the_timestamp_recheck_is_a_live_read(settings: Settings) -> None:
    """If it read ctx.data_timestamp instead, the check could never fail — SPEC §7 theatre."""
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    assert upstream.version_reads >= 1


# --- budgets -------------------------------------------------------------------------------


async def test_the_fan_out_refuses_before_spending_a_partial_wave(settings: Settings) -> None:
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream, budget=4)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)
    before = len(upstream.requests)

    with pytest.raises(BudgetExhausted) as caught:
        await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    # Six live phase values, the MISSING probe, and the timestamp recheck.
    assert "needs 8 upstream requests" in str(caught.value)
    assert len(upstream.requests) == before, "refused after issuing requests, not before"


async def test_the_budget_counts_the_timestamp_recheck(settings: Settings) -> None:
    """It is a real conditional request; omitting it understates traffic once per group-by."""
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    # One preflight, six phase buckets, and the MISSING probe.
    studies = len([r for r in upstream.requests if r.url.path.endswith("/studies")])
    assert studies == 8
    assert upstream.version_reads == 1
    assert ctx.spent == studies + upstream.version_reads == 9


def test_a_budget_failure_is_a_504() -> None:
    error = budget_error(BudgetExhausted("the 10000ms request budget elapsed before the fan-out"))

    assert error.code is ErrorCode.UPSTREAM_TIMEOUT
    assert error.status == 504


def test_an_elapsed_deadline_refuses_and_says_so() -> None:
    ctx = RunContext(
        client=None,  # type: ignore[arg-type]
        vocab=None,  # type: ignore[arg-type]
        options=Options(),
        deadline=time.monotonic() - 1,
        upstream_budget=40,
        data_timestamp=DATA_TIMESTAMP,
        budget_ms=10_000,
    )

    with pytest.raises(BudgetExhausted, match="10000ms request budget elapsed"):
        ctx.spend(1, "the preflight count")


# --- max_buckets clamping -------------------------------------------------------------------


async def test_too_many_buckets_are_clamped_and_the_clamp_is_reported(settings: Settings) -> None:
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream, options=Options(max_buckets=3))
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    assert len(bucketset.buckets) == 3
    assert any("max_buckets is 3" in warning for warning in bucketset.warnings)


# --- year dimensions ------------------------------------------------------------------------


async def test_year_buckets_use_date_ranges(settings: Settings) -> None:
    predicates = {
        f"AREA[StartDate]RANGE[{year}-01-01,{year}-12-31]": year - 2000
        for year in range(2020, 2023)
    } | {"AREA[StartDate]MISSING": 4, None: 100}
    upstream = Upstream(predicates)
    ctx = await a_context(settings, upstream)
    plan = AnalysisPlan(
        intent=Intent.TREND,
        filters=StudyFilter(start_year=2020, end_year=2022),
        group_by=GroupBy(dimension="start_year", bin=Bin(size=1)),
        interpretation="Annual count of clinical trials by start year.",
    )
    dim = REGISTRY["start_year"]

    bucketset = await counts.run(plan, dim, ctx, params={}, total=100)

    assert [bucket.key for bucket in bucketset.buckets] == ["2020", "2021", "2022"]
    assert [int(bucket.value) for bucket in bucketset.buckets] == [20, 21, 22]
    assert bucketset.semantics == "partition"


async def test_multi_year_bins_span_the_whole_window(settings: Settings) -> None:
    predicates = {
        "AREA[StartDate]RANGE[2015-01-01,2019-12-31]": 10,
        "AREA[StartDate]RANGE[2020-01-01,2024-12-31]": 20,
        "AREA[StartDate]MISSING": 0,
        None: 30,
    }
    upstream = Upstream(predicates)
    ctx = await a_context(settings, upstream)
    plan = AnalysisPlan(
        intent=Intent.TREND,
        filters=StudyFilter(start_year=2015, end_year=2024),
        group_by=GroupBy(dimension="start_year", bin=Bin(size=5)),
        interpretation="Five-year count of clinical trials by start year.",
    )

    bucketset = await counts.run(plan, REGISTRY["start_year"], ctx, params={}, total=30)

    assert [bucket.key for bucket in bucketset.buckets] == ["2015", "2020"]


# --- coverage math --------------------------------------------------------------------------


def a_bucketset(**overrides: Any) -> BucketSet:
    base: dict[str, Any] = {
        "buckets": [
            Bucket(key=key, label=key, value=value, exactness="exact")
            for key, value in A1_BUCKETS.items()
        ],
        "total": A1_TOTAL,
        "unclassified": A1_MISSING,
        "semantics": "overlapping",
        "mode": "server_counts",
    }
    return BucketSet(**{**base, **overrides})


def test_overlap_note_states_the_computed_integers() -> None:
    coverage, _ = build_coverage(a_bucketset(), REGISTRY["phase"])

    assert coverage.overlap_note is not None
    assert f"{A1_WITH_VALUE:,}" in coverage.overlap_note
    assert f"{A1_SUM:,}" in coverage.overlap_note
    assert f"overlap {A1_OVERLAP:,}" in coverage.overlap_note
    assert "do not sum to the total" in coverage.overlap_note


def test_overlap_note_matches_the_spec_shape() -> None:
    """SPEC §4.3 quotes this sentence; the numbers are computed, the wording is not."""
    coverage, _ = build_coverage(a_bucketset(), REGISTRY["phase"])

    assert coverage.overlap_note == (
        "phases is multi-valued: 2,758 studies carry \u22651 phase and contribute 3,273 bucket "
        "memberships (overlap 515). Bucket counts are each exact; they do not sum to the total."
    )


def test_zero_overlap_is_stated_rather_than_omitted() -> None:
    """An absent note reads as a partition, which is a different claim."""
    bucketset = a_bucketset(
        buckets=[Bucket(key="NA", label="NA", value=2_758, exactness="exact")],
    )

    coverage, _ = build_coverage(bucketset, REGISTRY["phase"])

    assert coverage.overlap_note is not None
    assert "overlap 0" in coverage.overlap_note
    # The number alone is not the disclosure: without this, the note is indistinguishable from a
    # partition, which is a stronger claim than the data supports.
    assert "multi-valued" in coverage.overlap_note
    assert "not of the dimension" in coverage.overlap_note


def test_a_partition_that_reconciles_is_quiet() -> None:
    bucketset = a_bucketset(
        buckets=[
            Bucket(key="INTERVENTIONAL", label="Interventional", value=90, exactness="exact"),
            Bucket(key="OBSERVATIONAL", label="Observational", value=5, exactness="exact"),
        ],
        total=100,
        unclassified=5,
        semantics="partition",
    )

    coverage, warnings = build_coverage(bucketset, REGISTRY["study_type"])

    assert coverage.groupby_semantics == "partition"
    assert coverage.overlap_note is None
    assert warnings == []


def test_a_partition_that_does_not_reconcile_warns_with_both_numbers() -> None:
    """Not rounding, not smoothed over: a partition that misses is a bug or an upstream change."""
    bucketset = a_bucketset(
        buckets=[Bucket(key="INTERVENTIONAL", label="Interventional", value=90, exactness="exact")],
        total=100,
        unclassified=5,
        semantics="partition",
    )

    _, warnings = build_coverage(bucketset, REGISTRY["study_type"])

    assert len(warnings) == 1
    assert "90" in warnings[0]
    assert "100" in warnings[0]
    assert "unexplained" in warnings[0]


def test_coverage_never_emits_a_truncated_key() -> None:
    """SPEC §4.3 forbids a bare `truncated: true` anywhere in the output."""
    coverage, _ = build_coverage(a_bucketset(), REGISTRY["phase"])

    assert "truncated" not in coverage.model_dump_json()


def test_overlapping_coverage_carries_no_share_field() -> None:
    coverage, _ = build_coverage(a_bucketset(), REGISTRY["phase"])
    serialized = coverage.model_dump_json()

    for forbidden in ("share_of_total", "percentage", "percent"):
        assert forbidden not in serialized
