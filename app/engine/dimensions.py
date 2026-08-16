"""The groupable dimensions, one frozen row each. SPEC §5.1 verbatim.

Adding a question type means adding a row here, not a code path.

Two traps the rows encode, both of which would produce a confident wrong chart:

- **Open vocabulary and non-partition are independent axes.** `lead_sponsor` has an open
  vocabulary (51,610 distinct names, notes §6.5) yet is a true partition, because a study has
  exactly one lead sponsor. Collapsing the two into one flag would wrongly mark it
  overlapping and suppress legitimate share-of-total rendering.
- **`enum_name` is not `area`.** `/studies/enums` publishes a `pieces` list per enum type,
  and it is not 1:1: `Status` governs `OverallStatus` *and* `LastKnownStatus` (notes §7).
  `LastKnownStatus` is populated for exactly the 95,740 `UNKNOWN` studies (notes §6.6), so
  grouping on it instead would silently answer a different question.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from app.errors import CheironError, ErrorCode


@dataclass(frozen=True)
class Dimension:
    key: str
    area: str
    enum_name: str | None
    record_path: str
    is_list: bool
    partition: bool
    label: str
    query_param: str | None


REGISTRY: Final[Mapping[str, Dimension]] = {
    dimension.key: dimension
    for dimension in (
        Dimension(
            key="phase",
            area="Phase",
            enum_name="Phase",
            record_path="protocolSection.designModule.phases",
            is_list=True,
            partition=False,
            label="Trial phase",
            query_param=None,
        ),
        Dimension(
            key="overall_status",
            area="OverallStatus",
            enum_name="Status",
            record_path="protocolSection.statusModule.overallStatus",
            is_list=False,
            partition=True,
            label="Recruitment status",
            query_param=None,
        ),
        Dimension(
            key="study_type",
            area="StudyType",
            enum_name="StudyType",
            record_path="protocolSection.designModule.studyType",
            is_list=False,
            partition=True,
            label="Study type",
            query_param=None,
        ),
        Dimension(
            key="sponsor_class",
            area="LeadSponsorClass",
            enum_name="AgencyClass",
            record_path="protocolSection.sponsorCollaboratorsModule.leadSponsor.class",
            is_list=False,
            partition=True,
            label="Lead sponsor class",
            query_param=None,
        ),
        Dimension(
            key="intervention_type",
            area="InterventionType",
            enum_name="InterventionType",
            record_path="protocolSection.armsInterventionsModule.interventions[].type",
            is_list=True,
            partition=False,
            label="Intervention type",
            query_param=None,
        ),
        Dimension(
            key="start_year",
            area="StartDate",
            enum_name=None,
            record_path="protocolSection.statusModule.startDateStruct.date",
            is_list=False,
            partition=True,
            label="Start year",
            query_param=None,
        ),
        Dimension(
            key="country",
            area="LocationCountry",
            enum_name=None,
            record_path="protocolSection.contactsLocationsModule.locations[].country",
            is_list=True,
            partition=False,
            label="Country",
            query_param="query.locn",
        ),
        Dimension(
            key="lead_sponsor",
            area="LeadSponsorName",
            enum_name=None,
            record_path="protocolSection.sponsorCollaboratorsModule.leadSponsor.name",
            is_list=False,
            partition=True,
            label="Lead sponsor",
            query_param="query.lead",
        ),
        Dimension(
            key="intervention_name",
            area="InterventionName",
            enum_name=None,
            record_path="protocolSection.armsInterventionsModule.interventions[].name",
            is_list=True,
            partition=False,
            label="Intervention",
            query_param="query.intr",
        ),
        Dimension(
            key="condition",
            area="Condition",
            enum_name=None,
            record_path="protocolSection.conditionsModule.conditions",
            is_list=True,
            partition=False,
            label="Condition",
            query_param="query.cond",
        ),
    )
}

TEMPORAL_KEYS: Final = frozenset({"start_year"})

QUANTITATIVE_KEYS: Final[frozenset[str]] = frozenset()
"""Dimensions whose buckets are numeric ranges rather than categories.

Empty on purpose: SPEC §5.1's ten rows are closed enums, open vocabularies, and one derived
date range, so there is no quantitative dimension to group by. `EnrollmentCount` would be the
obvious first one.

This is not trivia — SPEC §6.1's `histogram` and `scatter_plot` rows both require a
quantitative dimension, so both chart types are unreachable while this set is empty. The
planner refuses those intents by consulting this set rather than hardcoding the refusal, so
adding a row here makes them live instead of leaving a contradiction to find later.
"""


def resolve(key: str) -> Dimension:
    try:
        return REGISTRY[key]
    except KeyError:
        raise CheironError(
            ErrorCode.UNPLANNABLE_QUERY,
            f"{key!r} is not a dimension this service can group by.",
            details=[{"suggestion": f"Try one of: {', '.join(sorted(REGISTRY))}."}],
        ) from None


def is_temporal(dim: Dimension) -> bool:
    return dim.key in TEMPORAL_KEYS
