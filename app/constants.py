"""Upstream facts that are load-bearing enough to have exactly one spelling.

See `docs/CTG-API-NOTES.md` §2 for the live verification behind each value.
"""

FULL_MATCH_OP = "COVERAGE"
"""Essie exact-match operator, used as ``AREA[<field>]COVERAGE[FullMatch]"<label>"``.

``COVER`` is an exact grammar alias and returns identical counts; ``COVERAGE`` is the
spelling in SPEC §5.3 and in the official caveat. The ``AREA[<field>]`` prefix is part of
the predicate, not decoration: the same expression without it searches the default areas
and returns 4,591 instead of 1,841 at HTTP 200.
"""
