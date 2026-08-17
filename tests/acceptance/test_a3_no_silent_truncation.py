"""A3: no silent truncation. SPEC §8.

Above the record-mode threshold on an open vocabulary the service must say what it sampled,
how much of the corpus that was, and which half of the answer is exact.
"""

from __future__ import annotations

import urllib.parse

from app.config import Settings
from tests.acceptance.conftest import analyze, assert_contract
from tests.unit.test_sampled import default_upstream

QUESTION = {"query": "Which sponsors run the most trials?", "condition": "cancer"}


def test_a3_declares_the_mode_and_the_sample(settings: Settings) -> None:
    upstream = default_upstream()
    response = analyze(settings, upstream.handler(), QUESTION)
    assert_contract(response)

    coverage = response.json()["meta"]["coverage"]
    assert coverage["aggregation_mode"] == "sampled_then_confirmed"
    assert coverage["sample_size"] is not None
    assert coverage["sample_coverage"] is not None


def test_a3_warns_that_the_label_set_may_be_incomplete(settings: Settings) -> None:
    upstream = default_upstream()
    warnings = analyze(settings, upstream.handler(), QUESTION).json()["meta"]["warnings"]

    disclosure = next(w for w in warnings if "may be missing from this chart" in w)
    assert "Each displayed count is exact" in disclosure
    assert "% of" in disclosure


def test_a3_page_size_is_never_sent_above_1000(settings: Settings) -> None:
    """notes §3: upstream clamps silently, so exceeding it samples less than we disclose."""
    upstream = default_upstream()
    body = analyze(settings, upstream.handler(), {**QUESTION, "options": {"explain": True}}).json()

    sizes = [
        int(size[0])
        for url in body["meta"]["api_query_log"]
        if (size := urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("pageSize"))
    ]
    assert sizes
    assert max(sizes) <= 1_000
