"""Live enum vocabulary and the human labels rendered from it.

Values come from `/studies/enums` and are **never hardcoded** (SPEC §3, notes §7) — the
lists in the notes are reference only. Labels are the opposite: they are code, never model
output (SPEC §4.1), so they live here as a deterministic rule plus a small override map.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from app.ctg.client import CTGClient

logger = logging.getLogger("cheiron.vocab")

TTL_SECONDS: Final = 6 * 60 * 60

MISSING = "MISSING"
"""Our synthetic bucket for `AREA[<field>]MISSING`. Never an upstream enum value."""

PHASE_ORDER: Final = (
    "EARLY_PHASE1",
    "PHASE1",
    "PHASE2",
    "PHASE3",
    "PHASE4",
    "NA",
    MISSING,
)
"""Clinical order, matching SPEC §4's `sort` array. Upstream declares `NA` first."""

_ACRONYMS: Final = frozenset({"NIH", "FDA", "CDC", "VA", "AHRQ", "SAMHSA", "US", "EU", "UK"})

_OVERRIDES: Final[Mapping[tuple[str, str], str]] = {
    # The rule cannot produce a trailing digit break or an inner comma.
    ("Phase", "NA"): "Not Applicable",
    ("Phase", "EARLY_PHASE1"): "Early Phase 1",
    ("Phase", "PHASE1"): "Phase 1",
    ("Phase", "PHASE2"): "Phase 2",
    ("Phase", "PHASE3"): "Phase 3",
    ("Phase", "PHASE4"): "Phase 4",
    ("Status", "ACTIVE_NOT_RECRUITING"): "Active, not recruiting",
    # Notes §6.6: 16% of the corpus. "Unknown" alone reads as missing data rather than
    # "stopped updating", and it is upstream's own wording.
    ("Status", "UNKNOWN"): "Unknown status",
    ("AgencyClass", "NIH"): "NIH",
    ("AgencyClass", "FED"): "FED",
    ("AgencyClass", "OTHER_GOV"): "Other government",
    ("AgencyClass", "INDIV"): "Individual",
    ("AgencyClass", "AMBIG"): "Ambiguous",
}

_GLOBAL_OVERRIDES: Final[Mapping[str, str]] = {MISSING: "Not reported"}


def humanise(value: str) -> str:
    """`ENROLLING_BY_INVITATION` -> `Enrolling by invitation`. Sentence case, acronyms kept."""
    words = [word for word in value.split("_") if word]
    if not words:
        return value

    rendered = []
    for index, word in enumerate(words):
        if word in _ACRONYMS:
            rendered.append(word)
        elif index == 0:
            rendered.append(word.capitalize())
        else:
            rendered.append(word.lower())
    return " ".join(rendered)


@dataclass(frozen=True)
class Vocabulary:
    values_by_enum: Mapping[str, tuple[str, ...]]
    loaded_at: float

    @classmethod
    async def load(
        cls, client: CTGClient, *, clock: Callable[[], float] = time.monotonic
    ) -> Vocabulary:
        raw = await client.enums()
        return cls(
            values_by_enum={name: tuple(values) for name, values in raw.items()},
            loaded_at=clock(),
        )

    def values(self, enum_name: str) -> tuple[str, ...]:
        try:
            return self.values_by_enum[enum_name]
        except KeyError:
            raise KeyError(
                f"{enum_name!r} is not one of the {len(self.values_by_enum)} enums "
                "published by /studies/enums"
            ) from None

    def is_valid(self, enum_name: str, value: str) -> bool:
        return value in self.values_by_enum.get(enum_name, ())

    def label(self, enum_name: str, value: str) -> str:
        override = _OVERRIDES.get((enum_name, value)) or _GLOBAL_OVERRIDES.get(value)
        return override if override is not None else humanise(value)

    def sort_order(self, enum_name: str) -> tuple[str, ...]:
        if enum_name == "Phase":
            return PHASE_ORDER
        return self.values(enum_name)

    def is_stale(self, *, now: float, ttl_seconds: float = TTL_SECONDS) -> bool:
        return now - self.loaded_at >= ttl_seconds


class VocabularyCache:
    """Holds the loaded vocabulary for `TTL_SECONDS` and survives a failed refresh.

    An unreachable `/studies/enums` at startup must not take the process down (T02): the
    cache simply stays empty and `/health` reports it.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._vocabulary: Vocabulary | None = None

    @property
    def loaded(self) -> bool:
        return self._vocabulary is not None

    async def get(self, client: CTGClient) -> Vocabulary:
        current = self._vocabulary
        if current is not None and not current.is_stale(
            now=self._clock(), ttl_seconds=self._ttl_seconds
        ):
            return current

        try:
            refreshed = await Vocabulary.load(client, clock=self._clock)
        except Exception:
            if current is None:
                raise
            # A stale vocabulary beats no vocabulary; enum values change on the order of years.
            logger.warning("vocabulary refresh failed; serving the cached copy", exc_info=True)
            return current

        self._vocabulary = refreshed
        return refreshed

    async def warm(self, client: CTGClient) -> bool:
        """Best-effort startup load. Returns whether a vocabulary is available."""
        try:
            await self.get(client)
        except Exception:
            logger.warning("could not load /studies/enums at startup", exc_info=True)
            return False
        return True
