"""Live smoke check against ClinicalTrials.gov. The only code here that touches the network.

    python scripts/verify_upstream.py                # dataset version + headline counts
    python scripts/verify_upstream.py --predicates   # the whole notes §2 table, builder-built

`--predicates` sends **only** `Essie` output — no hand-written query strings — so a drift
between the builder and `docs/CTG-API-NOTES.md` shows up here rather than in a chart.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

from app.config import Settings
from app.ctg.client import CTGClient, CTGTransport
from app.ctg.essie import Essie
from app.ctg.vocab import Vocabulary

MERCK = "Merck Sharp & Dohme LLC"

# (label, expression built by Essie, count recorded in notes §2)
PREDICATES: list[tuple[str, str, int]] = [
    ("whole corpus", Essie.all_(), 598_690),
    ("field match", Essie.field_eq("Phase", "PHASE3"), 49_659),
    ("explicit NA phase", Essie.field_eq("Phase", "NA"), 234_433),
    ("absent field", Essie.missing("Phase"), 141_903),
    ("partial date", Essie.field_eq("StartDate", "2022"), 37_619),
    ("list size", Essie.field_eq("Phase:size", "2"), 24_549),
    (
        "date range",
        Essie.date_range("StartDate", date(2020, 1, 1), date(2020, 12, 31)),
        33_574,
    ),
    ("numeric range", Essie.numeric_range("EnrollmentCount", 500, "MAX"), 66_859),
    ("has-value", Essie.has_value("ResultsFirstPostDate"), 79_695),
    ("exact field match", Essie.full_match("LeadSponsorName", MERCK), 1_841),
    # A2's contrast: the same area without FullMatch substring-matches Merck KGaA and others.
    ("substring (no operator)", Essie.field_eq("LeadSponsorName", "Merck"), 2_733),
    ("distance", Essie.distance("LocationGeoPoint", 42.36, -71.06, 50), 29_070),
    ("phrase", Essie.phrase("breast cancer"), 17_819),
    (
        "boolean + grouping",
        Essie.not_(
            Essie.and_(
                Essie.or_(Essie.phrase("head"), Essie.phrase("neck")),
                Essie.phrase("pain"),
            ),
            Essie.phrase("cancer"),
        ),
        9_964,
    ),
]

# notes §2 rows the T03 builder API deliberately cannot express. Listed rather than dropped:
# a verifier that silently skips rows is the same failure as a chart with a missing bar.
NOT_BUILDER_EXPRESSIBLE: list[tuple[str, str]] = [
    ("Location scoping", "needs a SEARCH[<area>] helper; no consumer in SPEC 5.1-5.3"),
]


async def run_predicates(client: CTGClient) -> int:
    print("notes §2 predicates, every expression built by app.ctg.essie")
    print()

    drifted = 0
    for label, expression, recorded in PREDICATES:
        actual = await client.count({"filter.advanced": expression})
        delta = actual - recorded
        marker = "ok   " if delta == 0 else "DRIFT"
        drifted += delta != 0
        print(f"{marker} {label:<24} {actual:>9,}  (notes {recorded:>9,}  {delta:+,})")
        print(f"      {expression}")

    print()
    print("not expressible with the T03 builder API (so not asserted here):")
    for label, reason in NOT_BUILDER_EXPRESSIBLE:
        print(f"  --  {label:<24} {reason}")

    return drifted


async def run_headline(client: CTGClient) -> int:
    version = await client.version()
    print(f"apiVersion     {version.api_version}")
    print(f"dataTimestamp  {version.data_timestamp}")

    vocabulary = await Vocabulary.load(client)
    print(f"enums          {len(vocabulary.values_by_enum)} types")
    print(f"Phase          {', '.join(vocabulary.values('Phase'))}")
    print()

    drifted = 0
    for label, expression, recorded in PREDICATES[:4]:
        actual = await client.count({"filter.advanced": expression})
        delta = actual - recorded
        drifted += delta != 0
        marker = "ok   " if delta == 0 else "DRIFT"
        print(f"{marker} {label:<24} {actual:>9,}  (notes {recorded:>9,}  {delta:+,})")
    return drifted


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--predicates",
        action="store_true",
        help="verify every builder-expressible predicate in notes §2",
    )
    args = parser.parse_args()

    settings = Settings(_env_file=None, llm_enabled=False)

    async with CTGTransport(settings) as transport:
        client = CTGClient(transport)
        drifted = await (run_predicates(client) if args.predicates else run_headline(client))

    print()
    if drifted:
        print(f"{drifted} count(s) drifted from docs/CTG-API-NOTES.md.")
        print("Daily refresh drift is expected; a large gap means the note is wrong.")
        return 1
    print("All recorded counts still hold.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
