"""`POST /analyze` request models. SPEC §2.

`extra="forbid"` is load-bearing, not hygiene: SPEC §2.1 requires unknown top-level fields to
be **rejected**, because a typo'd filter that silently does nothing is worse than an error.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.errors import CheironError, ErrorCode

if TYPE_CHECKING:
    from app.ctg.vocab import Vocabulary

# field name -> the `/studies/enums` type that governs it (SPEC §2.1).
ENUM_BY_FIELD: dict[str, str] = {
    "phase": "Phase",
    "status": "Status",
    "study_type": "StudyType",
}


class Options(BaseModel):
    """SPEC §2.2. Defaults are the documented ones, so an absent `options` behaves identically."""

    model_config = ConfigDict(extra="forbid")

    max_buckets: int = Field(default=20, ge=1, le=100)
    include_citations: bool = True
    citations_per_datum: int = Field(default=3, ge=0, le=10)
    explain: bool = False


class AnalyzeRequest(BaseModel):
    """SPEC §2.1. Structured fields are hard constraints that override anything inferred."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=1000)
    drug_name: str | None = Field(default=None, max_length=200)
    condition: str | None = Field(default=None, max_length=200)
    sponsor: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, max_length=100)
    phase: list[str] | None = None
    status: list[str] | None = None
    study_type: str | None = None
    start_year: int | None = Field(default=None, ge=1900, le=2100)
    end_year: int | None = Field(default=None, ge=1900, le=2100)
    options: Options = Field(default_factory=Options)

    @field_validator("query", mode="before")
    @classmethod
    def _strip_query(cls, value: Any) -> Any:
        """Length is measured after strip, so `"  ab  "` is two characters, not six."""
        return value.strip() if isinstance(value, str) else value

    @field_validator("drug_name", "condition", "sponsor", "country", mode="before")
    @classmethod
    def _strip_hint(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def _years_are_ordered(self) -> Self:
        start, end = self.start_year, self.end_year
        if start is not None and end is not None and start > end:
            raise ValueError(f"start_year {start} must not exceed end_year {end}")
        return self

    def validate_against(self, vocab: Vocabulary) -> None:
        """Check enum-valued fields against the **live** vocabulary (SPEC §2.1).

        Pydantic cannot reach the async loader, and the enums are explicitly never hardcoded
        (notes §7), so the route calls this once the vocabulary is in hand.
        """
        details: list[dict[str, Any]] = []

        for field_name, enum_name in ENUM_BY_FIELD.items():
            raw = getattr(self, field_name)
            if raw is None:
                continue
            values = raw if isinstance(raw, list) else [raw]
            for value in values:
                if not vocab.is_valid(enum_name, value):
                    details.append(
                        {
                            "field": field_name,
                            "value": value,
                            "message": f"not a valid {enum_name} value",
                            "allowed": list(vocab.values(enum_name)),
                        }
                    )

        if details:
            raise CheironError(
                ErrorCode.INVALID_REQUEST,
                "One or more filters are not valid ClinicalTrials.gov enum values.",
                details=details,
            )
