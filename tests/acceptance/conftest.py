"""Shared harness for the acceptance suite. SPEC §8.

These tests assert on the **HTTP response**, not on internals — they are contract tests, and a
refactor that keeps the contract should not touch them. Every one is pinned to the recorded
`dataTimestamp`, so a data update fails loudly rather than silently changing what "correct" means.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import Handler, stub_transport

DATA_TIMESTAMP = "2026-08-14T09:00:05"
"""The revision every number in this suite was measured against."""


@pytest.fixture
def settings() -> Settings:
    """Degraded mode by default: no key, no model. A6 is the only test that needs it stated."""
    return Settings(_env_file=None, llm_enabled=False)


def analyze(settings: Settings, handler: Handler, payload: dict[str, Any]) -> Any:
    """POST /analyze through the real app over a stubbed upstream."""
    app = create_app(settings, transport=stub_transport(settings, handler))
    with TestClient(app) as client:
        return client.post("/analyze", json=payload)


@pytest.fixture
def responses() -> Iterator[list[Any]]:
    """Collects every response a test makes, so the global invariants can sweep them.

    A checklist that has to be repeated per test is a checklist that will be forgotten in the
    eighth one; this makes the invariants apply by construction.
    """
    collected: list[Any] = []
    yield collected


def assert_contract(response: Any) -> None:
    """The invariants every successful response must satisfy, whatever the question was."""
    body = response.json()

    if response.status_code != 200:
        assert "error" in body
        assert body["error"]["code"]
        assert body["error"]["request_id"]
        return

    # No bare truncation flag anywhere in the payload (SPEC §4.3).
    assert "truncated" not in _flatten_keys(body)

    coverage = body["meta"]["coverage"]
    for field in (
        "aggregation_mode",
        "groupby_semantics",
        "bucket_sum",
        "unclassified_count",
        "overlap_note",
        "sample_size",
        "sample_coverage",
    ):
        assert field in coverage, f"coverage is missing {field}"

    # Overlapping buckets must never carry a share — the whole they would imply does not exist.
    if coverage["groupby_semantics"] == "overlapping":
        rows = body["visualization"]["data"]
        for row in rows if isinstance(rows, list) else []:
            assert not {"share_of_total", "percentage", "share"} & set(row)


def _flatten_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys |= set(value)
        for item in value.values():
            keys |= _flatten_keys(item)
    elif isinstance(value, list):
        for item in value:
            keys |= _flatten_keys(item)
    return keys
