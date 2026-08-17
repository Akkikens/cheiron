"""A1 — reconciliation. SPEC §8.

`query.intr=pembrolizumab` grouped by phase must reconcile to a stated arithmetic:

    total 2,927 · MISSING 169 · with ≥1 phase 2,758
    NA 53 · EARLY_PHASE1 51 · PHASE1 1,039 · PHASE2 1,750 · PHASE3 363 · PHASE4 17
    Σ 3,273 · overlap 515

The buckets legitimately sum to more than the total because `phases` is multi-valued. That is
the finding, not a bug — and reporting it in numbers is what separates this from a chart that
quietly does not add up.
"""

from __future__ import annotations

from app.config import Settings
from tests.acceptance.conftest import analyze, assert_contract
from tests.unit.test_engine_counts import A1_BUCKETS, A1_MISSING, A1_SUM, A1_TOTAL, a1_upstream

QUESTION = {"query": "How many trials by phase?", "drug_name": "Pembrolizumab"}


def test_a1_buckets_reconcile(settings: Settings) -> None:
    upstream = a1_upstream()
    response = analyze(settings, upstream.handler(), QUESTION)
    assert_contract(response)

    body = response.json()
    rows = {row["phase"]: row["study_count"] for row in body["visualization"]["data"]}
    coverage = body["meta"]["coverage"]

    assert rows == A1_BUCKETS
    assert body["meta"]["total_matching_studies"] == A1_TOTAL
    assert coverage["bucket_sum"] == A1_SUM
    assert coverage["unclassified_count"] == A1_MISSING
    assert coverage["groupby_semantics"] == "overlapping"


def test_a1_overlap_note_states_the_actual_numbers(settings: Settings) -> None:
    """A bare "buckets overlap" is not acceptable; the note must quantify it."""
    upstream = a1_upstream()
    body = analyze(settings, upstream.handler(), QUESTION).json()

    note = body["meta"]["coverage"]["overlap_note"]
    assert note is not None
    for number in ("2,758", "3,273", "515"):
        assert number in note
    assert "do not sum to the total" in note


def test_a1_carries_no_share_of_total(settings: Settings) -> None:
    """Overlapping buckets have no whole to be a share of."""
    upstream = a1_upstream()
    body = analyze(settings, upstream.handler(), QUESTION).json()

    for row in body["visualization"]["data"]:
        assert not {"share_of_total", "percentage", "share"} & set(row)


def test_a1_keeps_na_and_missing_distinct(settings: Settings) -> None:
    """notes §6.1: 234,433 studies say NA; 141,903 have no phases field. Two buckets."""
    upstream = a1_upstream()
    body = analyze(settings, upstream.handler(), QUESTION).json()

    rows = {row["phase"] for row in body["visualization"]["data"]}
    assert "NA" in rows
    assert body["meta"]["coverage"]["unclassified_count"] == A1_MISSING
    assert "MISSING" not in rows  # unclassified is reported in coverage, not as a bar
