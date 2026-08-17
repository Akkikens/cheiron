"""Caches. SPEC §7.

Two of them, with different keys because they answer different questions:

- The **plan cache** maps a normalized question to an `AnalysisPlan`. A repeat question skips
  the model entirely, which is the "it is cheap" property in SPEC §1's table.
- The **result cache** maps `(plan.normalized_key(), data_timestamp)` to a finished response.
  The timestamp in the key is what makes it correct: the TTL is only housekeeping, because
  upstream refreshes on weekdays around 14:00 UTC and a stale entry would otherwise outlive
  the dataset it describes.

Question text is normalized by stripping, collapsing whitespace, and case-folding, and nothing
further. Stemming or stopword removal would make `"trials in France"` and `"trials for France"`
collide, and while they probably mean the same thing, "probably" is not a good enough reason to
serve one question's numbers under another question's name.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from cachetools import TTLCache

_WHITESPACE = re.compile(r"\s+")

PLAN_TTL_SECONDS = 24 * 60 * 60
RESULT_TTL_SECONDS = 24 * 60 * 60


class Cache[T](Protocol):
    def get(self, key: str) -> T | None: ...
    def set(self, key: str, value: T) -> None: ...


class TTLStore[T]:
    """A `Cache` with hit/miss counters, so caching is observable rather than assumed."""

    def __init__(self, *, maxsize: int = 512, ttl: int = PLAN_TTL_SECONDS) -> None:
        self._entries: TTLCache[str, T] = TTLCache(maxsize=maxsize, ttl=ttl)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> T | None:
        value = self._entries.get(key)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def set(self, key: str, value: T) -> None:
        self._entries[key] = value

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "entries": len(self._entries)}


def normalize_question(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip()).casefold()


def plan_cache_key(question: str, structured: dict[str, Any]) -> str:
    """Question plus the structured hints, since the hints change the plan."""
    parts = [normalize_question(question)]
    for field in sorted(structured):
        value = structured[field]
        if value is None or (isinstance(value, list) and not value):
            continue
        rendered = (
            ",".join(sorted(str(item) for item in value)) if isinstance(value, list) else value
        )
        parts.append(f"{field}={str(rendered).casefold()}")
    return "|".join(parts)


def result_cache_key(plan_key: str, data_timestamp: str, options_key: str = "") -> str:
    """Plan + dataset revision + the options that change the response body.

    `options` is load-bearing, not decoration. `max_buckets`, `include_citations`,
    `citations_per_datum` and `explain` all reshape the payload, so a key without them serves one
    caller's answer to another: an `explain=false` request could receive a cached response
    carrying `meta.api_query_log` and the full `meta.plan` it never asked for, and a
    `max_buckets=3` response could be served to a caller who asked for 20.
    """
    return f"{plan_key}@{data_timestamp}#{options_key}"


def options_cache_key(options: Any) -> str:
    """The subset of `options` that changes the response body."""
    return (
        f"b={options.max_buckets},c={int(options.include_citations)},"
        f"n={options.citations_per_datum},e={int(options.explain)}"
    )
