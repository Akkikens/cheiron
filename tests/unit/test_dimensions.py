"""The registry is SPEC §5.1; these tests are the spec table restated as assertions."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from app.engine.dimensions import REGISTRY, Dimension, is_temporal, resolve
from app.errors import CheironError, ErrorCode
from tests.conftest import load_fixture

# SPEC §5.1, transcribed by hand: (key, vocabulary is closed, partition).
SPEC_TABLE = [
    ("phase", True, False),
    ("overall_status", True, True),
    ("study_type", True, True),
    ("sponsor_class", True, True),
    ("intervention_type", True, False),
    ("start_year", True, True),
    ("country", False, False),
    ("lead_sponsor", False, True),
    ("intervention_name", False, False),
    ("condition", False, False),
]

ENUMS = load_fixture("studies_enums.json")
PIECES_BY_ENUM = {entry["type"]: entry["pieces"] for entry in ENUMS}
VALUES_BY_ENUM = {entry["type"]: [v["value"] for v in entry["values"]] for entry in ENUMS}
STUDY = load_fixture("study_full.json")


def resolve_record_path(record: Any, path: str) -> Any:
    """Walk a dotted `record_path`, stepping into the first element at each `[]`."""
    current = record
    for segment in path.split("."):
        listed = segment.endswith("[]")
        key = segment[:-2] if listed else segment
        if not isinstance(current, dict) or key not in current:
            raise KeyError(f"{path}: no {key!r}")
        current = current[key]
        if listed:
            if not isinstance(current, list) or not current:
                raise KeyError(f"{path}: {key!r} is not a non-empty list")
            current = current[0]
    return current


def test_registry_has_exactly_the_spec_rows() -> None:
    assert list(REGISTRY) == [key for key, _, _ in SPEC_TABLE]


@pytest.mark.parametrize(("key", "closed_vocab", "partition"), SPEC_TABLE)
def test_row_matches_spec(key: str, closed_vocab: bool, partition: bool) -> None:
    dim = REGISTRY[key]
    assert (dim.enum_name is not None or key == "start_year") is closed_vocab
    assert dim.partition is partition


def test_open_vocabulary_and_partition_are_independent_axes() -> None:
    """SPEC §5.1's trap: lead_sponsor is open-vocabulary yet a true partition."""
    lead_sponsor = REGISTRY["lead_sponsor"]
    assert lead_sponsor.enum_name is None
    assert lead_sponsor.partition is True

    phase = REGISTRY["phase"]
    assert phase.enum_name == "Phase"
    assert phase.partition is False


def test_multi_valued_rows_are_the_non_partitions() -> None:
    """A list-valued field cannot be a partition; buckets overlap (notes §5)."""
    for dim in REGISTRY.values():
        if dim.is_list:
            assert not dim.partition, f"{dim.key} is list-valued but claims to partition"


def test_exactly_the_documented_dimensions_overlap() -> None:
    overlapping = {dim.key for dim in REGISTRY.values() if not dim.partition}
    assert overlapping == {
        "phase",
        "intervention_type",
        "country",
        "intervention_name",
        "condition",
    }


# --- the enum_name / area distinction T02 measured ----------------------------------------


def test_every_enum_name_exists_upstream() -> None:
    for dim in REGISTRY.values():
        if dim.enum_name is not None:
            assert dim.enum_name in VALUES_BY_ENUM, f"{dim.key}: unknown enum {dim.enum_name}"


def test_area_is_a_piece_of_its_enum_type() -> None:
    """Catches an area/enum_name mismatch here rather than as a confidently wrong chart."""
    for dim in REGISTRY.values():
        if dim.enum_name is None:
            continue
        pieces = PIECES_BY_ENUM[dim.enum_name]
        assert dim.area in pieces, (
            f"{dim.key}: area {dim.area!r} is not governed by enum {dim.enum_name!r} "
            f"(pieces: {pieces})"
        )


def test_enum_type_name_is_not_reused_as_an_area() -> None:
    """`Status` and `AgencyClass` govern several areas, so the type name is not an area."""
    assert PIECES_BY_ENUM["Status"] == ["OverallStatus", "LastKnownStatus"]
    assert REGISTRY["overall_status"].enum_name == "Status"
    assert REGISTRY["overall_status"].area == "OverallStatus"


def test_status_dimension_is_not_last_known_status() -> None:
    """notes §6.6: LastKnownStatus covers exactly the 95,740 UNKNOWN studies."""
    assert REGISTRY["overall_status"].area != "LastKnownStatus"


def test_sponsor_class_uses_the_lead_sponsor_piece() -> None:
    """AgencyClass also governs OrgClass and CollaboratorClass — different questions."""
    assert "LeadSponsorClass" in PIECES_BY_ENUM["AgencyClass"]
    assert REGISTRY["sponsor_class"].area == "LeadSponsorClass"


# --- record paths against a recorded full study -------------------------------------------


@pytest.mark.parametrize("key", list(REGISTRY))
def test_record_path_resolves_in_a_real_study(key: str) -> None:
    value = resolve_record_path(STUDY, REGISTRY[key].record_path)
    assert value not in (None, "", [], {})


def test_list_flag_matches_the_record(key: str = "phase") -> None:
    phases = resolve_record_path(STUDY, REGISTRY[key].record_path)
    assert isinstance(phases, list) is REGISTRY[key].is_list


def test_single_valued_rows_are_scalars_in_the_record() -> None:
    for dim in REGISTRY.values():
        if dim.is_list or dim.record_path.endswith("[]"):
            continue
        if "[]" in dim.record_path:
            continue
        value = resolve_record_path(STUDY, dim.record_path)
        assert not isinstance(value, list), f"{dim.key} is marked single-valued but is a list"


# --- resolve / is_temporal ----------------------------------------------------------------


def test_resolve_returns_the_row() -> None:
    assert resolve("phase") is REGISTRY["phase"]


def test_unknown_dimension_is_unplannable_not_a_crash() -> None:
    with pytest.raises(CheironError) as caught:
        resolve("astrological_sign")

    error = caught.value
    assert error.code is ErrorCode.UNPLANNABLE_QUERY
    assert error.status == 422
    assert "phase" in error.details[0]["suggestion"]


def test_only_start_year_is_temporal() -> None:
    temporal = {dim.key for dim in REGISTRY.values() if is_temporal(dim)}
    assert temporal == {"start_year"}


def test_rows_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        REGISTRY["phase"].partition = True  # type: ignore[misc]


def test_dimension_keys_match_their_registry_slot() -> None:
    for key, dim in REGISTRY.items():
        assert isinstance(dim, Dimension)
        assert dim.key == key
