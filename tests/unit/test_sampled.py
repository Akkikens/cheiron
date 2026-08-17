"""`sampled_then_confirmed`. SPEC §5.2, §4.3, A2, A3.

The counts below are live measurements from notes §2: `AREA[LeadSponsorName]"Merck"` matches
2,733 by substring where the exact count for "Merck Sharp & Dohme LLC" is 1,841. The gap is the
whole reason this mode confirms every discovered label instead of reporting what the sample saw.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.essie import Essie
from app.ctg.vocab import Vocabulary
from app.engine.basefilter import base_filter
from app.engine.context import RunContext, new_context
from app.engine.dimensions import REGISTRY
from app.engine.modes import sampled
from app.errors import CheironError, ErrorCode
from app.models.plan import AnalysisPlan, GroupBy, Intent, Metric, StudyFilter
from app.models.request import Options
from tests.conftest import Handler, fixture_text, stub_transport

DATA_TIMESTAMP = "2026-08-14T09:00:05"

TOTAL = 57_400
"""Above the 2,000 record-mode threshold, so an open dimension lands in this mode."""

MERCK_FULL = "Merck Sharp & Dohme LLC"
MERCK_EXACT = 1_841
MERCK_SUBSTRING = 2_733
"""notes §2: what `AREA[LeadSponsorName]"Merck"` returns without FullMatch."""

SPONSORS = {
    MERCK_FULL: 1_841,
    "Pfizer": 3_862,
    "Novartis Pharmaceuticals": 2_104,
    "Hoffmann-La Roche": 1_502,
}


def a_plan(dimension: str = "lead_sponsor") -> AnalysisPlan:
    return AnalysisPlan(
        intent=Intent.DISTRIBUTION,
        filters=StudyFilter(condition="cancer"),
        group_by=GroupBy(dimension=dimension),
        metric=Metric.STUDY_COUNT,
        interpretation="Distribution of cancer trials across lead sponsors.",
    )


def sponsor_study(nct: str, name: str) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {"nctId": nct},
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": name}},
        }
    }


class Upstream:
    """Serves discovery pages and confirmation counts, and refuses anything unstubbed.

    Discovery and confirmation are deliberately different shapes here — a page request carries no
    `filter.advanced` and no `countTotal`; a confirmation carries both a predicate and
    `countTotal=true`. Keying on that split is what lets a test assert the mode never confuses
    a sample frequency for a count.
    """

    def __init__(
        self,
        pages: list[list[dict[str, Any]]],
        counts_by_predicate: dict[str, int],
        *,
        fail_predicates: dict[str, Exception] | None = None,
    ) -> None:
        self.pages = pages
        self.counts_by_predicate = counts_by_predicate
        self.fail_predicates = fail_predicates or {}
        self.requests: list[httpx.Request] = []

    def handler(self) -> Handler:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            path = request.url.path

            if path.endswith("/studies/enums"):
                return httpx.Response(200, text=fixture_text("studies_enums.json"))
            if path.endswith("/version"):
                return httpx.Response(
                    200, json={"apiVersion": "2.0.5", "dataTimestamp": DATA_TIMESTAMP}
                )

            predicate = request.url.params.get("filter.advanced")
            if request.url.params.get("countTotal") == "true":
                if predicate in self.fail_predicates:
                    raise self.fail_predicates[predicate]
                if predicate is None:
                    return httpx.Response(200, json={"totalCount": TOTAL, "studies": []})
                if predicate not in self.counts_by_predicate:
                    return httpx.Response(400, text=f"unstubbed predicate: {predicate}")
                return httpx.Response(
                    200, json={"totalCount": self.counts_by_predicate[predicate], "studies": []}
                )

            if predicate is not None:
                # A citation fetch under a bucket predicate.
                return httpx.Response(200, json={"studies": []})

            index = int(request.url.params.get("pageToken", "0"))
            if index >= len(self.pages):
                return httpx.Response(200, json={"studies": []})
            payload: dict[str, Any] = {"studies": self.pages[index]}
            if index + 1 < len(self.pages):
                payload["nextPageToken"] = str(index + 1)
            return httpx.Response(200, json=payload)

        return handle

    @property
    def page_sizes(self) -> list[int]:
        return [
            int(request.url.params["pageSize"])
            for request in self.requests
            if "pageSize" in request.url.params
        ]

    @property
    def confirmation_predicates(self) -> list[str]:
        return [
            request.url.params["filter.advanced"]
            for request in self.requests
            if request.url.params.get("countTotal") == "true"
            and "filter.advanced" in request.url.params
        ]


def default_upstream(**kwargs: Any) -> Upstream:
    """One discovery page whose frequency order deliberately differs from the true counts.

    Merck appears most often in the sample but is not the largest sponsor. If the mode ever
    reported sample frequencies, this fixture would show Merck first with the wrong number.
    """
    page = (
        [sponsor_study(f"NCT{i:08d}", MERCK_FULL) for i in range(6)]
        + [sponsor_study(f"NCT1{i:07d}", "Pfizer") for i in range(4)]
        + [sponsor_study(f"NCT2{i:07d}", "Novartis Pharmaceuticals") for i in range(3)]
        + [sponsor_study(f"NCT3{i:07d}", "Hoffmann-La Roche") for i in range(2)]
    )
    counts = {Essie.full_match("LeadSponsorName", name): count for name, count in SPONSORS.items()}
    counts[Essie.missing("LeadSponsorName")] = 412
    return Upstream([page], counts, **kwargs)


async def a_context(
    settings: Settings,
    upstream: Upstream,
    *,
    options: Options | None = None,
    budget: int = 40,
) -> RunContext:
    transport = stub_transport(settings, upstream.handler())
    client = CTGClient(transport)
    vocab = await Vocabulary.load(client)
    return new_context(
        client,
        vocab,
        options or Options(include_citations=False),
        settings=settings.model_copy(update={"max_upstream_requests": budget}),
        data_timestamp=DATA_TIMESTAMP,
    )


async def run_default(
    settings: Settings, upstream: Upstream, **kwargs: Any
) -> tuple[Any, RunContext]:
    ctx = await a_context(settings, upstream, **kwargs)
    params, _ = base_filter(a_plan().filters)
    bucketset = await sampled.run(
        a_plan(),
        REGISTRY["lead_sponsor"],
        ctx,
        params=params,
        total=TOTAL,
        sample_pages=3,
    )
    return bucketset, ctx


# --- A2: exact-match discipline -------------------------------------------------------------


async def test_a2_reports_the_exact_count_not_the_substring_count(settings: Settings) -> None:
    """SPEC A2: 1,841, never 2,733."""
    upstream = default_upstream()
    bucketset, _ = await run_default(settings, upstream)

    merck = next(bucket for bucket in bucketset.buckets if bucket.key == MERCK_FULL)
    assert int(merck.value) == MERCK_EXACT
    assert int(merck.value) != MERCK_SUBSTRING


async def test_every_confirmation_predicate_is_scoped_and_full_match(settings: Settings) -> None:
    """The operator and the AREA[] scope are both load-bearing (T01, T03)."""
    upstream = default_upstream()
    await run_default(settings, upstream)

    confirmations = [p for p in upstream.confirmation_predicates if "MISSING" not in p]
    assert confirmations
    for predicate in confirmations:
        assert predicate.startswith("AREA[LeadSponsorName]")
        assert "FullMatch" in predicate


async def test_confirmed_counts_win_over_sample_frequencies(settings: Settings) -> None:
    """The sample says Merck is largest; the corpus says Pfizer is. The corpus wins."""
    upstream = default_upstream()
    bucketset, _ = await run_default(settings, upstream)

    values = {bucket.key: int(bucket.value) for bucket in bucketset.buckets}
    assert values == SPONSORS
    assert bucketset.buckets[0].key == "Pfizer"  # ordered by confirmed count, not frequency


async def test_counts_are_exact_even_though_the_label_set_is_sampled(settings: Settings) -> None:
    upstream = default_upstream()
    bucketset, _ = await run_default(settings, upstream)

    assert {bucket.exactness for bucket in bucketset.buckets} == {"exact"}


# --- A3: no silent truncation ---------------------------------------------------------------


async def test_a3_discloses_the_sample(settings: Settings) -> None:
    upstream = default_upstream()
    bucketset, _ = await run_default(settings, upstream)

    assert bucketset.mode == "sampled_then_confirmed"
    assert bucketset.sample_size == 15
    assert bucketset.sample_coverage == round(15 / TOTAL, 3)
    assert any("may be missing from this chart" in warning for warning in bucketset.warnings)


async def test_a3_page_size_never_exceeds_1000(settings: Settings) -> None:
    """notes §3: upstream clamps silently, so a larger value would sample less than we claim."""
    upstream = default_upstream()
    await run_default(settings, upstream)

    assert upstream.page_sizes
    assert max(upstream.page_sizes) <= 1_000


async def test_disclosure_states_both_numbers_and_a_percentage(settings: Settings) -> None:
    sentence = sampled._disclosure(3_000, 57_400)

    assert "3,000-study sample" in sentence
    assert "5.2%" in sentence
    assert "57,400 matching studies" in sentence
    assert "Each displayed count is exact" in sentence


async def test_no_bare_truncation_flag_anywhere(settings: Settings) -> None:
    upstream = default_upstream()
    bucketset, _ = await run_default(settings, upstream)

    assert "truncated" not in " ".join(bucketset.warnings).lower()


# --- discovery ------------------------------------------------------------------------------


async def test_multiple_discovery_pages_are_walked_serially(settings: Settings) -> None:
    page_one = [sponsor_study(f"NCT{i:08d}", "Pfizer") for i in range(3)]
    page_two = [sponsor_study(f"NCT9{i:07d}", MERCK_FULL) for i in range(2)]
    counts = {
        Essie.full_match("LeadSponsorName", "Pfizer"): 3_862,
        Essie.full_match("LeadSponsorName", MERCK_FULL): MERCK_EXACT,
        Essie.missing("LeadSponsorName"): 412,
    }
    upstream = Upstream([page_one, page_two], counts)

    bucketset, _ = await run_default(settings, upstream)

    assert bucketset.sample_size == 5
    assert {bucket.key for bucket in bucketset.buckets} == {"Pfizer", MERCK_FULL}


async def test_sample_pages_bounds_the_walk(settings: Settings) -> None:
    pages = [[sponsor_study(f"NCT{page}{i:07d}", "Pfizer") for i in range(2)] for page in range(6)]
    counts = {
        Essie.full_match("LeadSponsorName", "Pfizer"): 3_862,
        Essie.missing("LeadSponsorName"): 412,
    }
    upstream = Upstream(pages, counts)
    ctx = await a_context(settings, upstream)
    params, _ = base_filter(a_plan().filters)

    bucketset = await sampled.run(
        a_plan(), REGISTRY["lead_sponsor"], ctx, params=params, total=TOTAL, sample_pages=2
    )

    assert bucketset.sample_size == 4  # two pages, not six


async def test_an_empty_sample_says_so_rather_than_returning_nothing(settings: Settings) -> None:
    counts = {Essie.missing("LeadSponsorName"): TOTAL}
    upstream = Upstream([[]], counts)

    bucketset, _ = await run_default(settings, upstream)

    assert bucketset.buckets == []
    assert any("no labels could be confirmed" in w for w in bucketset.warnings)


# --- confirmation edge cases ----------------------------------------------------------------


async def test_a_label_confirming_to_zero_is_dropped_and_named(settings: Settings) -> None:
    """Zero for a label the sample saw means a sampling artifact or an escaping fault."""
    page = [sponsor_study("NCT00000001", "Pfizer"), sponsor_study("NCT00000002", "Ghost Sponsor")]
    counts = {
        Essie.full_match("LeadSponsorName", "Pfizer"): 3_862,
        Essie.full_match("LeadSponsorName", "Ghost Sponsor"): 0,
        Essie.missing("LeadSponsorName"): 412,
    }
    upstream = Upstream([page], counts)

    bucketset, _ = await run_default(settings, upstream)

    assert [bucket.key for bucket in bucketset.buckets] == ["Pfizer"]
    dropped_warning = next(w for w in bucketset.warnings if "confirmed to zero" in w)
    assert "'Ghost Sponsor'" in dropped_warning
    assert "escaping fault" in dropped_warning


async def test_one_failed_confirmation_kills_the_whole_group_by(settings: Settings) -> None:
    """Same inversion as server_counts: a missing bar reads as a finding, not an outage."""
    upstream = default_upstream(
        fail_predicates={Essie.full_match("LeadSponsorName", "Pfizer"): httpx.ConnectError("boom")}
    )

    with pytest.raises(CheironError) as caught:
        await run_default(settings, upstream)

    # Surfaced as an upstream failure, not as a chart with three bars instead of four.
    assert caught.value.code is ErrorCode.UPSTREAM_ERROR


async def test_unclassified_is_probed_and_reported(settings: Settings) -> None:
    upstream = default_upstream()
    bucketset, _ = await run_default(settings, upstream)

    assert bucketset.unclassified == 412
    assert any("MISSING" in p for p in upstream.confirmation_predicates)


# --- budget ---------------------------------------------------------------------------------


async def test_budget_pressure_reduces_k_and_states_both_numbers(settings: Settings) -> None:
    upstream = default_upstream()
    options = Options(include_citations=False, max_buckets=4)
    # 1 discovery page + 2 reserved (MISSING probe, timestamp recheck) leaves room for 2.
    ctx = await a_context(settings, upstream, options=options, budget=5)
    params, _ = base_filter(a_plan().filters)

    bucketset = await sampled.run(
        a_plan(), REGISTRY["lead_sponsor"], ctx, params=params, total=TOTAL, sample_pages=1
    )

    assert len(bucketset.buckets) == 2
    reduction = next(w for w in bucketset.warnings if "top 2 of 4" in w)
    assert "budget" in reduction


# --- disclosure of what the numbers mean ----------------------------------------------------


async def test_free_text_and_coverage_caveats_are_recorded(settings: Settings) -> None:
    upstream = default_upstream()
    _, ctx = await run_default(settings, upstream)

    joined = " ".join(ctx.assumptions)
    assert "COVERAGE and EXPANSION are not fully implemented" in joined
    assert "51,610 distinct values" in joined
    assert "sponsor_class" in joined


async def test_country_does_not_get_the_sponsor_free_text_caveat(settings: Settings) -> None:
    """notes §6.7: FullMatch is a no-op on LocationCountry, and country names are normalized."""
    page = [
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT00000001"},
                "contactsLocationsModule": {"locations": [{"country": "France"}]},
            }
        }
    ]
    counts = {
        Essie.full_match("LocationCountry", "France"): 12_345,
        Essie.missing("LocationCountry"): 88,
    }
    upstream = Upstream([page], counts)
    ctx = await a_context(settings, upstream)
    plan = a_plan("country")
    params, _ = base_filter(plan.filters)

    bucketset = await sampled.run(
        plan, REGISTRY["country"], ctx, params=params, total=TOTAL, sample_pages=1
    )

    assert int(bucketset.buckets[0].value) == 12_345
    assert not any("51,610" in assumption for assumption in ctx.assumptions)
    # The operator is applied anyway: uniform beats per-dimension opt-out (T03, notes §6.7).
    assert all("FullMatch" in p for p in upstream.confirmation_predicates if "MISSING" not in p)


async def test_semantics_follow_the_registry_not_the_mode(settings: Settings) -> None:
    """lead_sponsor is open-vocabulary but still a partition: one lead sponsor per study."""
    upstream = default_upstream()
    bucketset, _ = await run_default(settings, upstream)

    assert bucketset.semantics == "partition"
