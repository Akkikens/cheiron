"""Pins T01 Part A's live findings against the recorded bodies (no network).

`docs/CTG-API-NOTES.md` §2 carries the prose; this file makes it executable.
"""

from __future__ import annotations

import json

import pytest

from app.constants import FULL_MATCH_OP
from tests.conftest import FIXTURES

UPSTREAM = FIXTURES / "upstream"
MANIFEST = json.loads((UPSTREAM / "fullmatch_manifest.json").read_text())
CASES = {case["fixture"]: case for case in MANIFEST["cases"]}


def _total(fixture: str) -> int:
    body = json.loads((UPSTREAM / fixture).read_text())
    return int(body["totalCount"])


def test_full_match_operator_is_settled() -> None:
    assert FULL_MATCH_OP == "COVERAGE"


def test_exact_match_count_is_1841() -> None:
    """A2's number, from the canonical predicate."""
    fixture = "fullmatch_coverage_full_name.json"
    assert CASES[fixture]["filter.advanced"] == (
        'AREA[LeadSponsorName]COVERAGE[FullMatch]"Merck Sharp & Dohme LLC"'
    )
    assert _total(fixture) == 1841


def test_cover_is_an_alias_of_coverage() -> None:
    assert _total("fullmatch_cover_alias_full_name.json") == _total(
        "fullmatch_coverage_full_name.json"
    )


def test_substring_match_inflates_the_count() -> None:
    """A2's wrong answer: 2733, because AREA[] alone substring-matches."""
    assert _total("fullmatch_no_operator_substring.json") == 2733


def test_full_name_without_the_operator_is_still_wrong() -> None:
    assert _total("fullmatch_no_operator_full_name.json") == 2170


def test_dropping_the_area_prefix_overcounts_silently() -> None:
    """Why the Essie builder must never emit COVERAGE outside an AREA[] context."""
    assert _total("fullmatch_missing_area_prefix.json") == 4591
    assert CASES["fullmatch_missing_area_prefix.json"]["http_status"] == 200


def test_full_match_on_a_partial_token_is_an_honest_zero() -> None:
    assert _total("fullmatch_coverage_bare_token.json") == 0


@pytest.mark.parametrize("case", MANIFEST["rejected_spellings"])
def test_misspelled_operators_fail_loudly(case: dict[str, object]) -> None:
    """Unlike aggFilters, a bad operator is a 400 — never a silent zero."""
    assert case["http_status"] == 400
    assert case["content_type"] == "text/plain"


@pytest.mark.parametrize("fixture", sorted(CASES))
def test_every_recorded_body_matches_its_manifest_entry(fixture: str) -> None:
    assert (UPSTREAM / fixture).exists()
    assert _total(fixture) == CASES[fixture]["total_count"]


def test_manifest_pins_the_dataset_version() -> None:
    assert MANIFEST["api_version"] == "2.0.5"
    assert MANIFEST["data_timestamp"] == "2026-08-14T09:00:05"


def test_no_fixture_is_undocumented() -> None:
    recorded = {p.name for p in UPSTREAM.glob("fullmatch_*.json")} - {"fullmatch_manifest.json"}
    assert recorded == set(CASES)
