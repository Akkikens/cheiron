"""SPEC §2's validation table, restated as assertions."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.ctg.client import CTGClient
from app.ctg.vocab import Vocabulary
from app.errors import CheironError, ErrorCode
from app.models.request import AnalyzeRequest, Options
from tests.conftest import Handler, stub_transport


@pytest.fixture
async def vocabulary(settings: Settings, enums_handler: Handler) -> Vocabulary:
    return await Vocabulary.load(CTGClient(stub_transport(settings, enums_handler)))


def test_minimal_request() -> None:
    request = AnalyzeRequest(query="trials by phase")

    assert request.query == "trials by phase"
    assert request.options.max_buckets == 20
    assert request.options.include_citations is True
    assert request.options.citations_per_datum == 3
    assert request.options.explain is False


def test_unknown_field_is_rejected() -> None:
    """SPEC §2.1: a typo'd filter that silently does nothing is worse than an error."""
    with pytest.raises(ValidationError) as caught:
        AnalyzeRequest(query="trials by phase", phaze=["PHASE2"])  # type: ignore[call-arg]

    errors = caught.value.errors()
    assert errors[0]["type"] == "extra_forbidden"
    assert errors[0]["loc"] == ("phaze",)


def test_unknown_option_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AnalyzeRequest(query="trials by phase", options={"maxBuckets": 5})


# --- query length is measured after strip -------------------------------------------------


def test_query_is_stripped_before_measuring() -> None:
    with pytest.raises(ValidationError, match="at least 3 characters"):
        AnalyzeRequest(query="  ab  ")


def test_whitespace_only_query_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AnalyzeRequest(query=" " * 50)


def test_stripped_query_is_stored_stripped() -> None:
    assert AnalyzeRequest(query="  trials by phase  ").query == "trials by phase"


def test_query_upper_bound() -> None:
    AnalyzeRequest(query="x" * 1000)
    with pytest.raises(ValidationError):
        AnalyzeRequest(query="x" * 1001)


# --- hint lengths and years ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "limit"),
    [("drug_name", 200), ("condition", 200), ("sponsor", 200), ("country", 100)],
)
def test_hint_length_limits(field: str, limit: int) -> None:
    AnalyzeRequest(query="trials by phase", **{field: "x" * limit})
    with pytest.raises(ValidationError):
        AnalyzeRequest(query="trials by phase", **{field: "x" * (limit + 1)})


def test_blank_hint_becomes_none() -> None:
    assert AnalyzeRequest(query="trials by phase", drug_name="   ").drug_name is None


def test_inverted_years_are_rejected() -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        AnalyzeRequest(query="trials over time", start_year=2020, end_year=2015)


def test_equal_years_are_allowed() -> None:
    assert AnalyzeRequest(query="trials in 2020", start_year=2020, end_year=2020).end_year == 2020


@pytest.mark.parametrize("year", [1899, 2101])
def test_year_bounds(year: int) -> None:
    with pytest.raises(ValidationError):
        AnalyzeRequest(query="trials over time", start_year=year)


# --- options bounds -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_buckets", 0),
        ("max_buckets", 101),
        ("citations_per_datum", -1),
        ("citations_per_datum", 11),
    ],
)
def test_option_bounds_are_rejected(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Options(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"), [("max_buckets", 1), ("max_buckets", 100), ("citations_per_datum", 0)]
)
def test_option_bounds_are_inclusive(field: str, value: int) -> None:
    assert getattr(Options(**{field: value}), field) == value


# --- live vocabulary validation (SPEC §2.1) -----------------------------------------------


async def test_valid_enums_pass(vocabulary: Vocabulary) -> None:
    request = AnalyzeRequest(
        query="trials by phase",
        phase=["PHASE2", "PHASE3"],
        status=["RECRUITING"],
        study_type="INTERVENTIONAL",
    )
    request.validate_against(vocabulary)


async def test_unknown_enum_value_is_invalid_request(vocabulary: Vocabulary) -> None:
    request = AnalyzeRequest(query="trials by phase", phase=["PHASE2", "PHASE9"])

    with pytest.raises(CheironError) as caught:
        request.validate_against(vocabulary)

    error = caught.value
    assert error.code is ErrorCode.INVALID_REQUEST
    assert error.status == 422
    assert [detail["value"] for detail in error.details] == ["PHASE9"]
    assert error.details[0]["field"] == "phase"
    assert "PHASE2" in error.details[0]["allowed"]


async def test_enum_validation_is_case_sensitive(vocabulary: Vocabulary) -> None:
    """notes §2: lowercase tokens are exactly how aggFilters silently returned 0."""
    with pytest.raises(CheironError):
        AnalyzeRequest(query="trials by phase", phase=["phase2"]).validate_against(vocabulary)


async def test_every_bad_value_is_reported_not_just_the_first(vocabulary: Vocabulary) -> None:
    request = AnalyzeRequest(query="trials by phase", phase=["NOPE1", "NOPE2"], study_type="NOPE3")

    with pytest.raises(CheironError) as caught:
        request.validate_against(vocabulary)

    assert [detail["value"] for detail in caught.value.details] == ["NOPE1", "NOPE2", "NOPE3"]


async def test_absent_enum_fields_are_not_validated(vocabulary: Vocabulary) -> None:
    AnalyzeRequest(query="trials by phase").validate_against(vocabulary)
