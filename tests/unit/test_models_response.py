"""SPEC §4: the response contract, including the two rules that fail plausibly if broken."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.models.response import (
    AnalyzeResponse,
    Channel,
    Citation,
    Coverage,
    Meta,
    Provenance,
    TimingMs,
    Visualization,
)

PHASE_X = {"field": "phase", "type": "nominal", "label": "Trial phase"}
COUNT_Y = {"field": "study_count", "type": "quantitative", "label": "Number of trials"}


def a_coverage(**overrides: Any) -> Coverage:
    base: dict[str, Any] = {
        "aggregation_mode": "server_counts",
        "groupby_semantics": "partition",
        "bucket_sum": 100,
        "unclassified_count": 0,
        "overlap_note": None,
        "sample_size": None,
        "sample_coverage": None,
    }
    return Coverage(**{**base, **overrides})


def a_visualization(**overrides: Any) -> Visualization:
    base: dict[str, Any] = {
        "type": "bar_chart",
        "title": "Pembrolizumab Trials by Phase",
        "encoding": {"x": PHASE_X, "y": COUNT_Y},
        "data": [{"phase": "PHASE2", "study_count": 1750}],
    }
    return Visualization(**{**base, **overrides})


def a_meta(**overrides: Any) -> Meta:
    base: dict[str, Any] = {
        "interpretation": "Distribution of pembrolizumab trials across phases.",
        "planner": "heuristic_fallback",
        "total_matching_studies": 2927,
        "coverage": a_coverage(),
        "provenance": Provenance(
            api_version="2.0.5",
            data_timestamp="2026-08-14T09:00:05",
            retrieved_at="2026-08-16T10:22:41Z",
        ),
        "timing_ms": TimingMs(plan=480, retrieve=610, total=1131),
    }
    return Meta(**{**base, **overrides})


def test_spec_example_response_parses() -> None:
    response = AnalyzeResponse(visualization=a_visualization(), meta=a_meta())

    assert response.visualization.type == "bar_chart"
    assert response.meta.provenance.source == "clinicaltrials.gov"


def test_citation_shape() -> None:
    citation = Citation(
        nct_id="NCT05053880",
        field="protocolSection.designModule.phases",
        excerpt='["PHASE1","PHASE2"]',
        url="https://clinicaltrials.gov/study/NCT05053880",
    )
    assert citation.nct_id == "NCT05053880"


def test_channel_rejects_an_unknown_type() -> None:
    with pytest.raises(ValidationError):
        Channel(field="phase", type="categorical", label="Trial phase")


# --- SPEC §4.1: every channel field exists in every row -----------------------------------


def test_channel_field_missing_from_a_row_is_rejected() -> None:
    with pytest.raises(ValidationError, match="missing from data row 1"):
        a_visualization(
            data=[
                {"phase": "PHASE2", "study_count": 1750},
                {"phase": "PHASE3"},
            ]
        )


def test_channel_field_absent_everywhere_is_rejected() -> None:
    with pytest.raises(ValidationError, match="study_count"):
        a_visualization(data=[{"phase": "PHASE2"}])


def test_empty_data_is_allowed() -> None:
    """A4: zero matching studies yields an empty data array, never a fabricated row."""
    assert a_visualization(data=[]).data == []


def test_table_columns_are_channels_too() -> None:
    visualization = a_visualization(
        type="table",
        encoding={"columns": [PHASE_X, COUNT_Y]},
        data=[{"phase": "PHASE2", "study_count": 1750}],
    )
    assert visualization.type == "table"

    with pytest.raises(ValidationError, match="columns\\[1\\]"):
        a_visualization(
            type="table",
            encoding={"columns": [PHASE_X, COUNT_Y]},
            data=[{"phase": "PHASE2"}],
        )


# --- network_graph is the documented exception ---------------------------------------------


def test_network_graph_carries_nodes_and_edges() -> None:
    visualization = a_visualization(
        type="network_graph",
        encoding={"nodes": {"id": "id"}, "edges": {"source": "source"}},
        data={"nodes": [{"id": "a"}], "edges": [{"source": "a", "target": "b"}]},
    )
    assert visualization.type == "network_graph"
    assert set(visualization.data) == {"nodes", "edges"}


NETWORK_ENCODING = {"nodes": {"id": "id"}, "edges": {"source": "source"}}


def test_network_graph_rejects_flat_rows() -> None:
    with pytest.raises(ValidationError, match="`data` must be exactly"):
        a_visualization(type="network_graph", encoding=NETWORK_ENCODING, data=[{"phase": "PHASE2"}])


def test_network_graph_rejects_a_stray_data_key() -> None:
    with pytest.raises(ValidationError, match="`data` must be exactly"):
        a_visualization(
            type="network_graph",
            encoding=NETWORK_ENCODING,
            data={"nodes": [], "edges": [], "clusters": []},
        )


def test_network_graph_rejects_channel_style_encoding() -> None:
    with pytest.raises(ValidationError, match="`encoding` must be exactly"):
        a_visualization(type="network_graph", data={"nodes": [], "edges": []})


def test_a_flat_type_rejects_dict_data() -> None:
    with pytest.raises(ValidationError, match="list of rows"):
        a_visualization(data={"nodes": [], "edges": []})


# --- SPEC §4.3: coverage is a hard requirement --------------------------------------------


def test_coverage_nullable_fields_are_required_to_be_present() -> None:
    with pytest.raises(ValidationError) as caught:
        Coverage(
            aggregation_mode="server_counts",
            groupby_semantics="partition",
            bucket_sum=1,
            unclassified_count=0,
        )
    missing = {error["loc"][0] for error in caught.value.errors()}
    assert missing == {"overlap_note", "sample_size", "sample_coverage"}


def test_overlapping_semantics_must_state_its_numbers() -> None:
    """A bare `truncated: true` is not acceptable anywhere in this API."""
    with pytest.raises(ValidationError, match="overlap_note"):
        a_coverage(groupby_semantics="overlapping", overlap_note=None)


def test_sampled_mode_must_disclose_the_sample() -> None:
    with pytest.raises(ValidationError, match="sample_size and sample_coverage"):
        a_coverage(aggregation_mode="sampled_then_confirmed")

    assert (
        a_coverage(
            aggregation_mode="sampled_then_confirmed",
            sample_size=500,
            sample_coverage=0.25,
        ).sample_coverage
        == 0.25
    )


def test_sample_coverage_is_a_fraction() -> None:
    with pytest.raises(ValidationError):
        a_coverage(sample_coverage=1.5)


def test_a1_coverage_shape() -> None:
    """A1's exact numbers for pembrolizumab grouped by phase."""
    coverage = a_coverage(
        groupby_semantics="overlapping",
        bucket_sum=3273,
        unclassified_count=169,
        overlap_note=(
            "phases is multi-valued: 2,758 studies carry >=1 phase and contribute 3,273 "
            "bucket memberships (overlap 515)."
        ),
    )
    assert coverage.bucket_sum == 3273
    assert coverage.unclassified_count == 169


# --- SPEC §6.1: no share under overlapping semantics --------------------------------------


@pytest.mark.parametrize("key", ["share_of_total", "share", "percentage", "percent", "pct"])
def test_share_key_is_rejected_under_overlapping_semantics(key: str) -> None:
    overlapping = a_coverage(
        groupby_semantics="overlapping",
        overlap_note="phases is multi-valued; buckets do not sum to the total.",
    )

    with pytest.raises(ValidationError, match="no denominator"):
        AnalyzeResponse(
            visualization=a_visualization(
                data=[{"phase": "PHASE2", "study_count": 1750, key: 0.6}]
            ),
            meta=a_meta(coverage=overlapping),
        )


def test_share_key_is_fine_under_a_partition() -> None:
    response = AnalyzeResponse(
        visualization=a_visualization(
            data=[{"phase": "PHASE2", "study_count": 1750, "share_of_total": 0.6}]
        ),
        meta=a_meta(),
    )
    assert response.visualization.data[0]["share_of_total"] == 0.6


def test_a1_response_carries_no_share_field() -> None:
    """A1: the response must not contain a share/percentage field at all."""
    response = AnalyzeResponse(
        visualization=a_visualization(data=[{"phase": "PHASE2", "study_count": 1750}]),
        meta=a_meta(
            coverage=a_coverage(
                groupby_semantics="overlapping",
                bucket_sum=3273,
                unclassified_count=169,
                overlap_note="phases is multi-valued; buckets do not sum to the total.",
            )
        ),
    )
    rows = response.visualization.data
    assert isinstance(rows, list)
    assert all("share_of_total" not in row for row in rows)


# --- meta ---------------------------------------------------------------------------------


def test_explain_only_fields_default_to_absent() -> None:
    meta = a_meta()
    assert meta.api_query_log is None
    assert meta.plan is None


def test_planner_name_is_constrained() -> None:
    with pytest.raises(ValidationError):
        a_meta(planner="gpt-4.1")


@pytest.mark.parametrize("planner", ["llm", "llm_repaired", "heuristic_fallback"])
def test_documented_planner_names(planner: str) -> None:
    assert a_meta(planner=planner).planner == planner
