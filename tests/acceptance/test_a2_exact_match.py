"""A2 — exact-match discipline. SPEC §8.

`AREA[LeadSponsorName]"Merck"` returns 2,733 by substring; the exact count for
"Merck Sharp & Dohme LLC" is 1,841 (notes §2). A sponsor chart must report 1,841.
"""

from __future__ import annotations

from app.config import Settings
from tests.acceptance.conftest import analyze, assert_contract
from tests.unit.test_sampled import MERCK_EXACT, MERCK_FULL, MERCK_SUBSTRING, default_upstream

QUESTION = {"query": "Which sponsors run the most trials?", "condition": "cancer"}


def test_a2_reports_the_exact_count(settings: Settings) -> None:
    upstream = default_upstream()
    response = analyze(settings, upstream.handler(), QUESTION)
    assert_contract(response)

    data = response.json()["visualization"]["data"]
    rows = {row["lead_sponsor"]: row["study_count"] for row in data}
    assert rows[MERCK_FULL] == MERCK_EXACT
    assert MERCK_SUBSTRING not in rows.values()


def test_a2_every_confirmation_is_full_match_scoped(settings: Settings) -> None:
    """The predicate is the evidence: FullMatch present, and scoped to an AREA."""
    upstream = default_upstream()
    body = analyze(settings, upstream.handler(), {**QUESTION, "options": {"explain": True}}).json()

    confirmations = [
        url
        for url in body["meta"]["api_query_log"]
        if "LeadSponsorName" in url and "MISSING" not in url
    ]
    assert confirmations
    for url in confirmations:
        assert "FullMatch" in url
        assert "AREA" in url
