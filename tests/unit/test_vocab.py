"""Vocabulary is loaded, never hardcoded; labels are code, never model output."""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import PHASE_ORDER, Vocabulary, VocabularyCache, humanise
from tests.conftest import Handler, load_fixture, stub_transport

# SPEC §4's `sort` array, transcribed by hand.
SPEC_PHASE_SORT = ["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA", "MISSING"]

# Upstream's own human labels, from the recorded /studies/enums `legacyValue` field.
LEGACY_LABELS = {
    entry["type"]: {value["value"]: value["legacyValue"] for value in entry["values"]}
    for entry in load_fixture("studies_enums.json")
}


@pytest.fixture
async def vocabulary(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


async def test_values_come_from_upstream(vocabulary: Vocabulary) -> None:
    assert vocabulary.values("Phase") == (
        "NA",
        "EARLY_PHASE1",
        "PHASE1",
        "PHASE2",
        "PHASE3",
        "PHASE4",
    )
    assert len(vocabulary.values_by_enum) == 41


async def test_unknown_enum_name_is_loud(vocabulary: Vocabulary) -> None:
    with pytest.raises(KeyError, match="not one of the 41 enums"):
        vocabulary.values("Phaze")


async def test_is_valid_rejects_unknown_values(vocabulary: Vocabulary) -> None:
    assert vocabulary.is_valid("Phase", "PHASE2")
    assert not vocabulary.is_valid("Phase", "phase2")
    assert not vocabulary.is_valid("Phase", "PHASE5")
    assert not vocabulary.is_valid("Nope", "PHASE2")


async def test_phase_sort_order_matches_spec(vocabulary: Vocabulary) -> None:
    assert list(vocabulary.sort_order("Phase")) == SPEC_PHASE_SORT
    assert list(PHASE_ORDER) == SPEC_PHASE_SORT


async def test_phase_sort_order_is_not_upstream_declaration_order(vocabulary: Vocabulary) -> None:
    """Upstream declares NA first; clinically it belongs second-to-last."""
    assert vocabulary.values("Phase")[0] == "NA"
    assert vocabulary.sort_order("Phase")[-2] == "NA"


async def test_other_enums_keep_declaration_order(vocabulary: Vocabulary) -> None:
    assert vocabulary.sort_order("StudyType") == vocabulary.values("StudyType")


@pytest.mark.parametrize("enum_name", ["Phase", "Status"])
async def test_labels_match_upstreams_own_wording(vocabulary: Vocabulary, enum_name: str) -> None:
    """Our rule + overrides reproduce `legacyValue` exactly for the enums we render."""
    for value in vocabulary.values(enum_name):
        assert vocabulary.label(enum_name, value) == LEGACY_LABELS[enum_name][value]


async def test_agency_class_labels_improve_on_upstream(vocabulary: Vocabulary) -> None:
    """AgencyClass `legacyValue` is just the value echoed back, so the rule wins there."""
    assert LEGACY_LABELS["AgencyClass"]["OTHER_GOV"] == "OTHER_GOV"
    assert vocabulary.label("AgencyClass", "OTHER_GOV") == "Other government"
    assert vocabulary.label("AgencyClass", "NIH") == "NIH"
    assert vocabulary.label("AgencyClass", "INDUSTRY") == "Industry"


async def test_unknown_means_different_things_in_different_enums(vocabulary: Vocabulary) -> None:
    """Notes §6.6: a Status of UNKNOWN is 16% of the corpus, not a missing value."""
    assert vocabulary.label("Status", "UNKNOWN") == "Unknown status"
    assert vocabulary.label("AgencyClass", "UNKNOWN") == "Unknown"


async def test_missing_bucket_has_a_label(vocabulary: Vocabulary) -> None:
    assert vocabulary.label("Phase", "MISSING") == "Not reported"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("INTERVENTIONAL", "Interventional"),
        ("ENROLLING_BY_INVITATION", "Enrolling by invitation"),
        ("DIETARY_SUPPLEMENT", "Dietary supplement"),
        ("NIH", "NIH"),
        ("", ""),
    ],
)
def test_humanise_rule(value: str, expected: str) -> None:
    assert humanise(value) == expected


async def test_cache_serves_one_load_within_the_ttl(
    settings: Settings, enums_handler: Handler
) -> None:
    calls = 0

    def counting(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return enums_handler(request)

    client = CTGClient(stub_transport(settings, counting))
    cache = VocabularyCache()

    first = await cache.get(client)
    second = await cache.get(client)

    assert first is second
    assert calls == 1


async def test_cache_reloads_once_stale(settings: Settings, enums_handler: Handler) -> None:
    now = [0.0]

    def counting(request: httpx.Request) -> httpx.Response:
        return enums_handler(request)

    client = CTGClient(stub_transport(settings, counting))
    cache = VocabularyCache(ttl_seconds=100.0, clock=lambda: now[0])

    first = await cache.get(client)
    now[0] = 101.0
    second = await cache.get(client)

    assert second is not first


async def test_a_failed_refresh_keeps_serving_the_stale_copy(
    settings: Settings, enums_handler: Handler
) -> None:
    """Enum values change on the order of years; a stale vocabulary beats none."""
    now = [0.0]
    healthy = True

    def flaky(request: httpx.Request) -> httpx.Response:
        if healthy:
            return enums_handler(request)
        return httpx.Response(503, text="down")

    client = CTGClient(stub_transport(settings, flaky, attempts=1))
    cache = VocabularyCache(ttl_seconds=100.0, clock=lambda: now[0])

    first = await cache.get(client)
    healthy = False
    now[0] = 101.0

    assert await cache.get(client) is first


async def test_warm_reports_failure_without_raising(settings: Settings) -> None:
    def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    cache = VocabularyCache()
    client = CTGClient(stub_transport(settings, failing, attempts=1))

    assert await cache.warm(client) is False
    assert cache.loaded is False
