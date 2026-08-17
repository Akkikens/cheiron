"""A4: no fabrication. SPEC §8.

A query matching zero studies returns an empty `data` array, `total_matching_studies: 0`, and a
warning. Never an invented row, and never an error. SPEC §4.4 requires a valid spec even when
the honest answer is "nothing".
"""

from __future__ import annotations

from app.config import Settings
from tests.acceptance.conftest import analyze, assert_contract
from tests.unit.test_engine_counts import Upstream

QUESTION = {"query": "How many trials by phase?", "drug_name": "Nonexistentmab"}


def empty_upstream() -> Upstream:
    return Upstream({None: 0})


def test_a4_returns_an_empty_spec_not_an_error(settings: Settings) -> None:
    response = analyze(settings, empty_upstream().handler(), QUESTION)
    assert_contract(response)

    assert response.status_code == 200
    body = response.json()
    assert body["visualization"]["data"] == []
    assert body["meta"]["total_matching_studies"] == 0


def test_a4_says_so_in_a_warning(settings: Settings) -> None:
    body = analyze(settings, empty_upstream().handler(), QUESTION).json()

    assert body["meta"]["warnings"]
    assert any("no studies" in w.lower() or "zero" in w.lower() for w in body["meta"]["warnings"])


def test_a4_still_returns_a_renderable_encoding(settings: Settings) -> None:
    """An empty chart is still a chart: a renderer must not have to special-case it."""
    body = analyze(settings, empty_upstream().handler(), QUESTION).json()

    encoding = body["visualization"]["encoding"]
    assert encoding
    assert body["visualization"]["type"]
