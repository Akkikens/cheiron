"""Citation sampling. SPEC §4.2. Rule 2 is the whole task."""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.engine.citations import (
    ORDERING_ASSUMPTION,
    citation_note,
    citations_from_records,
    fields_projection,
    sample_citations,
    serialize_excerpt,
    value_at,
)
from app.engine.dimensions import REGISTRY
from app.engine.modes import counts
from app.engine.preflight import preflight
from app.models.request import Options
from tests.conftest import load_fixture
from tests.unit.test_engine_counts import (
    A1_BUCKETS,
    a1_plan,
    a1_upstream,
    a_context,
)

STUDY = load_fixture("study_full.json")


def phase12_study() -> dict[str, Any]:
    """The SPEC §4.2 worked example: phases serialize to `["PHASE1","PHASE2"]`."""
    study = copy.deepcopy(STUDY)
    study["protocolSection"]["identificationModule"]["nctId"] = "NCT05053880"
    study["protocolSection"]["designModule"]["phases"] = ["PHASE1", "PHASE2"]
    return study


def a1_studies() -> dict[str, list[dict[str, Any]]]:
    """One projected study per phase bucket; PHASE2 carries the dual-phase excerpt."""
    out: dict[str, list[dict[str, Any]]] = {}
    for key in A1_BUCKETS:
        if key == "PHASE2":
            out[f"AREA[Phase]{key}"] = [phase12_study()]
        else:
            study = copy.deepcopy(STUDY)
            study["protocolSection"]["designModule"]["phases"] = [key]
            study["protocolSection"]["identificationModule"]["nctId"] = (
                f"NCT{hash(key) % 10**8:08d}"
            )
            out[f"AREA[Phase]{key}"] = [study]
    return out


def test_citation_note_sampled_wording() -> None:
    assert citation_note(3, 1_750) == "3 of 1,750 contributing studies"


def test_citation_note_all_wording() -> None:
    assert citation_note(4, 4) == "all 4 contributing studies"


def test_excerpt_for_phase2_is_verbatim_compact_json() -> None:
    study = phase12_study()
    excerpt = serialize_excerpt(value_at(study, REGISTRY["phase"].record_path))

    assert excerpt == '["PHASE1","PHASE2"]'
    assert json.loads(excerpt) == ["PHASE1", "PHASE2"]
    # Appears as a substring of the raw fixture once re-serialized the same way.
    assert excerpt in json.dumps(study, separators=(",", ":"))


def test_scalar_excerpt_appears_in_raw_bytes() -> None:
    study = STUDY
    path = REGISTRY["overall_status"].record_path
    excerpt = serialize_excerpt(value_at(study, path))
    raw = json.dumps(study)

    assert excerpt in raw
    assert excerpt == study["protocolSection"]["statusModule"]["overallStatus"]


def test_no_citation_field_names_a_free_text_field() -> None:
    citations, _ = citations_from_records([phase12_study()], REGISTRY["phase"], 1, 1_750)

    assert citations
    for citation in citations:
        lowered = citation.field.lower()
        assert "briefsummary" not in lowered
        assert "detaileddescription" not in lowered
        assert "brieftitle" not in lowered
        assert citation.field == REGISTRY["phase"].record_path


def test_every_registry_record_path_is_citation_safe() -> None:
    """The structural guarantee: no dimension's membership path is a narrative field."""
    for dim in REGISTRY.values():
        lowered = dim.record_path.lower()
        assert "briefsummary" not in lowered
        assert "detaileddescription" not in lowered


def test_fields_projection_never_asks_for_narrative() -> None:
    projection = fields_projection(REGISTRY["phase"]).lower()

    assert "nctid" in projection
    assert "briefsummary" not in projection
    assert "protocolsection.designmodule.phases" in projection


def test_url_is_constructed_not_taken_from_upstream() -> None:
    citations, _ = citations_from_records([phase12_study()], REGISTRY["phase"], 1, 10)

    assert citations[0].url == "https://clinicaltrials.gov/study/NCT05053880"


@pytest.mark.asyncio
async def test_in_memory_records_skip_the_network(settings: Settings) -> None:
    upstream = a1_upstream()
    ctx = await a_context(settings, upstream)
    before = len(upstream.requests)

    citations, note = await sample_citations(
        "AREA[Phase]PHASE2",
        REGISTRY["phase"],
        3,
        ctx,
        contributing=1_750,
        records=[phase12_study()] * 3,
    )

    assert len(citations) == 3
    assert note == "3 of 1,750 contributing studies"
    assert len(upstream.requests) == before


@pytest.mark.asyncio
async def test_a1_shaped_response_carries_citations(settings: Settings) -> None:
    upstream = a1_upstream(studies_by_predicate=a1_studies())
    ctx = await a_context(
        settings,
        upstream,
        options=Options(include_citations=True, citations_per_datum=3),
        budget=80,
    )
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    for bucket in bucketset.buckets:
        assert len(bucket.citations) <= 3
        if bucket.value > 0 and bucket.citations:
            for citation in bucket.citations:
                assert citation.nct_id
                assert citation.field == dim.record_path
                assert citation.excerpt
                assert citation.url.startswith("https://clinicaltrials.gov/study/")
            assert bucket.citation_note is not None

    phase2 = next(b for b in bucketset.buckets if b.key == "PHASE2")
    assert phase2.citations[0].excerpt == '["PHASE1","PHASE2"]'
    assert phase2.citation_note == "1 of 1,750 contributing studies"
    assert ORDERING_ASSUMPTION in ctx.assumptions


@pytest.mark.asyncio
async def test_include_citations_false_spends_nothing_on_pages(settings: Settings) -> None:
    upstream = a1_upstream(studies_by_predicate=a1_studies())
    ctx = await a_context(settings, upstream, options=Options(include_citations=False), budget=40)
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    page_requests = [
        r
        for r in upstream.requests
        if r.url.path.endswith("/studies") and r.url.params.get("countTotal") != "true"
    ]
    assert page_requests == []
    assert all(not bucket.citations for bucket in bucketset.buckets)
    assert ORDERING_ASSUMPTION not in ctx.assumptions


@pytest.mark.asyncio
async def test_citation_failure_keeps_the_numbers(settings: Settings) -> None:
    """Opposite of the count fan-out: evidence is nice-to-have, numbers are not."""
    upstream = a1_upstream(
        studies_by_predicate=a1_studies(),
        fail_citation_predicates={
            "AREA[Phase]PHASE3": httpx.ConnectError("citation upstream went away")
        },
    )
    ctx = await a_context(
        settings,
        upstream,
        options=Options(include_citations=True, citations_per_datum=3),
        budget=80,
    )
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    phase3 = next(b for b in bucketset.buckets if b.key == "PHASE3")
    assert int(phase3.value) == A1_BUCKETS["PHASE3"]
    assert phase3.citations == []
    assert any("PHASE3" in warning and "Citations" in warning for warning in bucketset.warnings)

    phase2 = next(b for b in bucketset.buckets if b.key == "PHASE2")
    assert phase2.citations  # siblings still cited


@pytest.mark.asyncio
async def test_tight_budget_cuts_citations_before_counts(settings: Settings) -> None:
    upstream = a1_upstream(studies_by_predicate=a1_studies())
    # 1 preflight + 6 buckets + 1 missing + 1 version = 9. Leave no room for 6 citation fetches.
    ctx = await a_context(
        settings,
        upstream,
        options=Options(include_citations=True, citations_per_datum=3),
        budget=9,
    )
    plan, dim = a1_plan(), REGISTRY["phase"]
    pre = await preflight(plan, dim, ctx, threshold=2_000)

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)

    assert {b.key: int(b.value) for b in bucketset.buckets} == A1_BUCKETS
    assert all(not b.citations for b in bucketset.buckets)
    assert any(
        "not for citations" in warning or "Citation fetches cut" in warning
        for warning in bucketset.warnings
    )
