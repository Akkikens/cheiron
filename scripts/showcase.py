"""Exercise every chart type against live ClinicalTrials.gov and print what came back.

    python scripts/showcase.py            # summary table
    python scripts/showcase.py --json     # full specifications, one per case

Ten chart types, ten real queries, no fixtures. This exists because "supports ten chart types"
is a claim, and a reviewer should be able to check it in one command rather than by reading the
registry.

Six cases run through the **deterministic planner** — no API key, no model. The other four need
intents the keyword matcher does not emit (a two-series comparison, a cross-tab, a scatter, a
single-value KPI), so their `AnalysisPlan` is supplied directly through the same
`ChatCompleter` seam the tests use. That is labelled per row: a plan the model would have
produced, standing in for the model, with every number still computed downstream by the engine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.analyze import analyze
from app.config import Settings
from app.ctg.client import CTGTransport
from app.ctg.vocab import VocabularyCache
from app.models.request import AnalyzeRequest
from app.models.response import AnalyzeResponse

GLIO = {"condition": "glioblastoma", "start_year": 2024, "end_year": 2025}
PEMBRO = {"drug_name": "Pembrolizumab"}


def plan(**overrides: Any) -> dict[str, Any]:
    """A complete `AnalysisPlan` payload, so a supplied plan is explicit rather than patched."""
    base: dict[str, Any] = {
        "intent": "distribution",
        "filters": {
            "condition": None,
            "intervention": None,
            "sponsor": None,
            "term": None,
            "country": None,
            "phase": [],
            "status": [],
            "study_type": None,
            "start_year": None,
            "end_year": None,
        },
        "series": [],
        "group_by": {"dimension": "phase", "bin": None},
        "secondary_group_by": None,
        "metric": "study_count",
        "viz_hint": None,
        "interpretation": "Distribution of clinical trials.",
    }
    filters = {**base["filters"], **overrides.pop("filters", {})}
    return {**base, **overrides, "filters": filters}


GLIO_FILTERS = {"condition": "glioblastoma", "start_year": 2024, "end_year": 2025}


@dataclass(frozen=True)
class Case:
    expect: str
    request: dict[str, Any]
    supplied_plan: dict[str, Any] | None = None
    note: str = ""


CASES: tuple[Case, ...] = (
    Case("bar_chart", {"query": "How many trials by phase?", **PEMBRO}),
    Case(
        "time_series",
        {
            "query": "How has this changed over time?",
            **PEMBRO,
            "start_year": 2019,
            "end_year": 2025,
        },
    ),
    Case(
        "choropleth_map",
        {"query": "Which countries run these trials?", "condition": "glioblastoma"},
    ),
    Case("histogram", {"query": "How big are these trials?", **GLIO}),
    Case("network_graph", {"query": "Which interventions are studied together?", **GLIO}),
    Case(
        "table",
        {"query": "Which conditions are studied?", **PEMBRO, "options": {"max_buckets": 3}},
        supplied_plan=plan(
            intent="list",
            filters={"intervention": "Pembrolizumab"},
            group_by={"dimension": "condition", "bin": None},
            interpretation="Conditions studied in pembrolizumab trials, as a list.",
        ),
        note="intent=list is always a table, whatever the cardinality",
    ),
    Case(
        "grouped_bar_chart",
        {"query": "Compare sponsors by phase", **PEMBRO},
        supplied_plan=plan(
            intent="comparison",
            filters={"intervention": "Pembrolizumab"},
            series=[
                {
                    "label": "Merck",
                    "filters": {**plan()["filters"], "sponsor": "Merck Sharp & Dohme LLC"},
                },
                {"label": "Pfizer", "filters": {**plan()["filters"], "sponsor": "Pfizer"}},
            ],
            interpretation="Phase distribution compared across two sponsors.",
        ),
        note="two real fan-outs, one per series",
    ),
    Case(
        "stacked_bar_chart",
        {"query": "Phase by status", **GLIO},
        supplied_plan=plan(
            filters=GLIO_FILTERS,
            group_by={"dimension": "phase", "bin": None},
            secondary_group_by={"dimension": "overall_status", "bin": None},
            interpretation="Phase distribution broken down by recruitment status.",
        ),
        note="cross-tab, free in record mode",
    ),
    Case(
        "scatter_plot",
        {"query": "Enrollment against start date", **GLIO},
        supplied_plan=plan(
            intent="scatter",
            filters=GLIO_FILTERS,
            group_by={"dimension": "enrollment_count", "bin": None},
            interpretation="Enrollment against start date, one point per study.",
        ),
        note="one point per study; record mode only",
    ),
    Case(
        "kpi",
        {
            "query": "How many phase 3 trials are recruiting?",
            **PEMBRO,
            "phase": ["PHASE3"],
            "status": ["RECRUITING"],
        },
        supplied_plan=plan(
            filters={
                "intervention": "Pembrolizumab",
                "phase": ["PHASE3"],
                "status": ["RECRUITING"],
            },
            group_by={"dimension": "study_type", "bin": None},
            interpretation="Count of recruiting phase 3 trials.",
        ),
        note="a single bucket renders as a KPI, not a one-bar chart",
    ),
)


@dataclass
class Result:
    case: Case
    response: AnalyzeResponse | None = None
    error: str = ""
    warnings: list[str] = field(default_factory=list)


def completer_for(payload: dict[str, Any]) -> Any:
    async def complete(messages: Sequence[dict[str, str]], schema: dict[str, Any]) -> str:
        return json.dumps(payload)

    return complete


async def run_case(case: Case, transport: CTGTransport, cache: VocabularyCache) -> Result:
    supplied = case.supplied_plan is not None
    settings = Settings(
        _env_file=None,
        llm_enabled=supplied,
        openai_api_key="sk-plan-supplied-directly" if supplied else None,
    )
    try:
        response = await analyze(
            AnalyzeRequest.model_validate(case.request),
            transport=transport,
            vocabulary_cache=cache,
            settings=settings,
            completer=completer_for(case.supplied_plan) if supplied else None,
        )
    except Exception as exc:
        return Result(case=case, error=f"{type(exc).__name__}: {exc}")
    return Result(case=case, response=response, warnings=list(response.meta.warnings))


def summarise(result: Result) -> str:
    case = result.case
    source = "plan supplied" if case.supplied_plan else "deterministic"
    if result.response is None:
        return f"FAIL  {case.expect:<18} {source:<14} {result.error[:60]}"

    viz = result.response.visualization
    data = viz.data
    size = len(data["nodes"]) if isinstance(data, dict) else len(data)
    mark = "ok  " if viz.type.value == case.expect else "DIFF"
    got = f"{viz.type.value}" if viz.type.value != case.expect else case.expect
    return (
        f"{mark}  {got:<18} {source:<14} {size:>4} items  "
        f"{result.response.meta.total_matching_studies:>7,} studies  "
        f"{result.response.meta.coverage.aggregation_mode}"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print each full specification")
    args = parser.parse_args()

    settings = Settings(_env_file=None, llm_enabled=False)
    transport = CTGTransport(settings)
    cache = VocabularyCache()

    print("Every chart type, against live ClinicalTrials.gov\n")
    failures = 0
    try:
        for case in CASES:
            result = await run_case(case, transport, cache)
            print(summarise(result))
            if case.note:
                print(f"      {case.note}")
            for warning in result.warnings:
                print(f"      warn: {warning[:110]}")
            if result.response is None or result.response.visualization.type.value != case.expect:
                failures += 1
            if args.json and result.response is not None:
                print(
                    json.dumps(result.response.model_dump(mode="json", exclude_none=True), indent=2)
                )
    finally:
        await transport.aclose()

    print(f"\n{len(CASES) - failures} of {len(CASES)} chart types produced as expected.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
