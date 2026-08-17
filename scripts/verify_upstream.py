"""Live smoke check against ClinicalTrials.gov. The only code here that touches the network.

    python scripts/verify_upstream.py                # dataset version + headline counts
    python scripts/verify_upstream.py --predicates   # the whole notes §2 table, builder-built
    python scripts/verify_upstream.py --a1           # SPEC A1 through the real engine, live

`--predicates` sends **only** `Essie` output, no hand-written query strings, so a drift
between the builder and `docs/CTG-API-NOTES.md` shows up here rather than in a chart.

`--a1` runs `preflight` and the `server_counts` fan-out against live upstream and checks the
reconciliation, not just the individual counts. Drift is tolerated here and nowhere else: the
pinned test in `tests/unit/test_engine_counts.py` must not move when upstream refreshes, or it
could no longer tell a data update from a broken predicate.
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
from app.engine.context import new_context
from app.engine.coverage import build_coverage
from app.engine.dimensions import REGISTRY
from app.engine.modes import counts
from app.engine.preflight import preflight
from app.models.plan import AnalysisPlan, GroupBy, Intent, Metric, StudyFilter
from app.models.request import Options

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


async def run_a1(client: CTGClient, settings: Settings) -> int:
    """SPEC A1 end-to-end at the engine layer: `query.intr=pembrolizumab` grouped by phase."""
    version = await client.version()
    vocabulary = await Vocabulary.load(client)
    ctx = new_context(
        client,
        vocabulary,
        Options(),
        settings=settings,
        data_timestamp=version.data_timestamp,
    )

    plan = AnalysisPlan(
        intent=Intent.DISTRIBUTION,
        filters=StudyFilter(intervention="pembrolizumab"),
        group_by=GroupBy(dimension="phase"),
        metric=Metric.STUDY_COUNT,
        interpretation="Distribution of clinical trials studying pembrolizumab across phases.",
    )
    dim = REGISTRY["phase"]

    pre = await preflight(plan, dim, ctx, threshold=settings.record_mode_threshold)
    print(f"dataTimestamp  {version.data_timestamp}")
    print(f"preflight      total {pre.total:,} -> mode {pre.mode}")
    print(f"params         {pre.params}")
    print()

    bucketset = await counts.run(plan, dim, ctx, params=pre.params, total=pre.total)
    coverage, warnings = build_coverage(bucketset, dim)

    for bucket in bucketset.buckets:
        print(f"  {bucket.key:<14} {bucket.label:<16} {int(bucket.value):>7,}")
    print(f"  {'MISSING':<14} {'Not reported':<16} {bucketset.unclassified:>7,}")
    print()

    with_value = bucketset.total - bucketset.unclassified
    overlap = bucketset.bucket_sum - with_value

    # The pair SPEC A1 quotes alongside the totals. It is the only number in that block that no
    # response can show, because the engine offers no phase-by-phase cross-tab, so it was quoted
    # for a long time with nothing verifying it. One count settles it, and it is worth having:
    # 472 of the 515 overlapping memberships are this one pairing, which is what a phase 1/2
    # registration looks like in the data.
    pair = await client.count(
        {
            **pre.params,
            "filter.advanced": Essie.and_(
                pre.params.get("filter.advanced") or Essie.all_(),
                Essie.field_eq("Phase", "PHASE1"),
                Essie.field_eq("Phase", "PHASE2"),
            ),
        }
    )

    recorded = {
        "total": 2_927,
        "unclassified": 169,
        "bucket_sum": 3_273,
        "overlap": 515,
        "PHASE1∩PHASE2": 472,
    }
    actual = {
        "total": bucketset.total,
        "unclassified": bucketset.unclassified,
        "bucket_sum": bucketset.bucket_sum,
        "overlap": overlap,
        "PHASE1∩PHASE2": pair,
    }

    drifted = 0
    for key, expected in recorded.items():
        delta = actual[key] - expected
        drifted += delta != 0
        marker = "ok   " if delta == 0 else "DRIFT"
        print(f"{marker} {key:<14} {actual[key]:>9,}  (SPEC A1 {expected:>7,}  {delta:+,})")

    # The identity, not the individual numbers: this must hold at any dataset revision.
    assert bucketset.bucket_sum - with_value == overlap
    print()
    print(f"semantics      {coverage.groupby_semantics}")
    print(f"overlap_note   {coverage.overlap_note}")
    if warnings:
        print(f"warnings       {warnings}")
    print(f"upstream spend {ctx.spent} requests")
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
    parser.add_argument(
        "--a1",
        action="store_true",
        help="run SPEC A1 through preflight and the count fan-out against live upstream",
    )
    args = parser.parse_args()

    settings = Settings(_env_file=None, llm_enabled=False)

    async with CTGTransport(settings) as transport:
        client = CTGClient(transport)
        if args.a1:
            drifted = await run_a1(client, settings)
        elif args.predicates:
            drifted = await run_predicates(client)
        else:
            drifted = await run_headline(client)

    print()
    if drifted:
        print(f"{drifted} count(s) drifted from docs/CTG-API-NOTES.md.")
        print("Daily refresh drift is expected; a large gap means the note is wrong.")
        return 1
    print("All recorded counts still hold.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
