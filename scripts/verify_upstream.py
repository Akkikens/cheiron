"""Live smoke check against ClinicalTrials.gov. The only code here that touches the network.

    python scripts/verify_upstream.py

Prints the current dataset version and re-runs the counts `docs/CTG-API-NOTES.md` asserts,
so a drifted note is visible immediately rather than at acceptance time.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import Settings
from app.constants import FULL_MATCH_OP
from app.ctg.client import CTGClient, CTGTransport
from app.ctg.vocab import Vocabulary

MERCK = "Merck Sharp & Dohme LLC"

# (label, filter.advanced, count recorded in the notes)
CHECKS: list[tuple[str, str, int]] = [
    ("whole corpus", "ALL", 598_690),
    (
        "exact sponsor match",
        f'AREA[LeadSponsorName]{FULL_MATCH_OP}[FullMatch]"{MERCK}"',
        1_841,
    ),
    ("substring sponsor match", 'AREA[LeadSponsorName]"Merck"', 2_733),
    ("explicit NA phase", "AREA[Phase]NA", 234_433),
    ("absent phase field", "AREA[Phase]MISSING", 141_903),
]


async def main() -> int:
    settings = Settings(_env_file=None, llm_enabled=False)

    async with CTGTransport(settings) as transport:
        client = CTGClient(transport)

        version = await client.version()
        print(f"apiVersion     {version.api_version}")
        print(f"dataTimestamp  {version.data_timestamp}")

        vocabulary = await Vocabulary.load(client)
        print(f"enums          {len(vocabulary.values_by_enum)} types")
        print(f"Phase          {', '.join(vocabulary.values('Phase'))}")
        print()

        drifted = 0
        for label, expression, recorded in CHECKS:
            actual = await client.count({"filter.advanced": expression})
            delta = actual - recorded
            marker = "ok " if delta == 0 else "DRIFT"
            print(f"{marker} {label:<24} {actual:>9,}  (notes: {recorded:>9,}  {delta:+,})")
            drifted += delta != 0

    print()
    if drifted:
        print(f"{drifted} count(s) drifted from docs/CTG-API-NOTES.md.")
        print("Daily refresh drift is expected; a large gap means the note is wrong.")
    else:
        print("All recorded counts still hold.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
