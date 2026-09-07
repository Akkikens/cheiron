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
        Dimension(
            key="enrollment_count",
            area="EnrollmentCount",
            enum_name=None,
            record_path="protocolSection.designModule.enrollmentInfo.count",
            is_list=False,
            partition=True,
            label="Enrollment",
            query_param=None,
        ),
    )
}

TEMPORAL_KEYS: Final = frozenset({"start_year"})

QUANTITATIVE_KEYS: Final[frozenset[str]] = frozenset({"enrollment_count"})
"""Dimensions whose values are numeric and therefore binnable.

Adding `enrollment_count` here is what makes SPEC §6.1's `histogram` and `scatter_plot` rows
reachable: both need a quantitative dimension, and until this set was non-empty the planner
refused those intents outright.
"""

ENROLLMENT_BINS: Final[tuple[tuple[int, int | None], ...]] = (
    (0, 10),
    (11, 50),
    (51, 100),
    (101, 500),
    (501, 1_000),
    (1_001, 5_000),
    (5_001, None),
)
"""Fixed edges, chosen for how trials are actually sized rather than by equal width.

Equal-width bins are useless here: enrollment spans 0 to 188,814,085 (notes §6.4), so linear
bins would put essentially every study in the first one. The open top bin absorbs the
`99999999` placeholders and the genuine outliers together, which is why record mode winsorizes
before computing enrollment *metrics*: the histogram counts studies, so it does not need to.
"""


def bin_key(value: int) -> str:
    """The bin a raw enrollment falls in, as its own label."""
    for low, high in ENROLLMENT_BINS:
        if high is None or value <= high:
            return bin_label(low, high)
    return bin_label(*ENROLLMENT_BINS[-1])


def bin_label(low: int, high: int | None) -> str:
    return f"{low:,}+" if high is None else f"{low:,}-{high:,}"


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
