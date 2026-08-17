"""Refresh `tests/fixtures/upstream/` from live calls. Run deliberately, never from tests.

    python scripts/record_fixtures.py

Everything written here is a verbatim upstream response, so the offline suite asserts
against real bytes. Public ClinicalTrials.gov data only, so no credentials are involved.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.config import Settings
from app.ctg.client import CTGClient, CTGTransport

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "upstream"

# A modern interventional trial, rich enough to exercise every dimension record_path.
STUDY_QUERY = {
    "query.intr": "pembrolizumab",
    "filter.advanced": "AREA[Phase]PHASE2",
    "pageSize": "5",
}


def write(name: str, payload: Any) -> None:
    path = FIXTURES / name
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path.relative_to(FIXTURES.parent.parent.parent)} ({path.stat().st_size:,} B)")


async def main() -> int:
    settings = Settings(_env_file=None, llm_enabled=False)

    async with CTGTransport(settings) as transport:
        client = CTGClient(transport)

        version = await client.version()
        write(
            "version.json",
            {"apiVersion": version.api_version, "dataTimestamp": version.data_timestamp},
        )

        page = await client.page(STUDY_QUERY)
        if not page.studies:
            print("no studies returned; refusing to write an empty fixture", file=sys.stderr)
            return 1
        write("study_full.json", page.studies[0])

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
