"""Co-occurrence network graphs. SPEC §5.4.

Available **only** in `complete_records`, where co-occurrence is computed from the full result
set and is therefore exact and unbiased. Sampled co-occurrence is not offered at all — a
network built from a relevance-ranked sample looks authoritative and isn't.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from app.engine.citations import nct_id_of, value_at
from app.engine.context import RunContext
from app.models.plan import AnalysisPlan, ChartType
from app.models.response import Visualization

Pairing = Literal[
    "sponsor_intervention",
    "intervention_intervention",
    "condition_intervention",
]


def choose_pairing(plan: AnalysisPlan) -> Pairing:
    """Pick a pairing from the filters present; default to sponsor ↔ intervention."""
    filters = plan.filters
    if filters.condition and not filters.sponsor:
        return "condition_intervention"
    if filters.intervention and not filters.sponsor:
        return "intervention_intervention"
    return "sponsor_intervention"


def build(
    studies: Sequence[Mapping[str, Any]],
    plan: AnalysisPlan,
    ctx: RunContext,
    *,
    pairing: Pairing | None = None,
) -> tuple[Visualization, list[str]]:
    """Return a network_graph visualization and any warnings."""
    warnings: list[str] = []
    chosen = pairing or choose_pairing(plan)
    edge_weights: Counter[tuple[str, str]] = Counter()
    edge_ncts: dict[tuple[str, str], list[str]] = {}
    node_weights: Counter[str] = Counter()
    node_groups: dict[str, str] = {}

    for study in studies:
        left, right, group_left, group_right = _endpoints(study, chosen)
        if not left or not right:
            continue
        nct = _safe_nct(study)
        for a in left:
            node_weights[a] += 1
            node_groups[a] = group_left
        for b in right:
            node_weights[b] += 1
            node_groups[b] = group_right

        for source, target in _pairs(left, right, chosen):
            key = (source, target) if source <= target else (target, source)
            edge_weights[key] += 1
            if nct:
                edge_ncts.setdefault(key, []).append(nct)

    # Drop single-occurrence edges first, then prune nodes by degree.
    kept_edges = {edge: weight for edge, weight in edge_weights.items() if weight >= 2}
    dropped_edges = len(edge_weights) - len(kept_edges)

    degree: Counter[str] = Counter()
    for (a, b), weight in kept_edges.items():
        degree[a] += weight
        degree[b] += weight

    ranked = sorted(degree, key=lambda node: (-degree[node], node))
    max_nodes = ctx.options.max_buckets
    kept_nodes = set(ranked[:max_nodes])
    dropped_nodes = max(0, len(ranked) - len(kept_nodes))

    final_edges = {
        edge: weight
        for edge, weight in kept_edges.items()
        if edge[0] in kept_nodes and edge[1] in kept_nodes
    }

    per_datum = ctx.options.citations_per_datum if ctx.options.include_citations else 0
    nodes = [
        {
            "id": node,
            "label": node,
            "group": node_groups.get(node, "unknown"),
            "weight": node_weights[node],
        }
        for node in sorted(kept_nodes, key=lambda n: (-degree[n], n))
    ]
    edges: list[dict[str, Any]] = []
    for (source, target), weight in sorted(final_edges.items(), key=lambda item: -item[1]):
        entry: dict[str, Any] = {"source": source, "target": target, "weight": weight}
        if per_datum > 0:
            ncts = edge_ncts.get((source, target), [])[:per_datum]
            entry["citations"] = [
                {
                    "nct_id": nct,
                    "field": "co-occurrence",
                    "excerpt": nct,
                    "url": f"https://clinicaltrials.gov/study/{nct}",
                }
                for nct in ncts
            ]
        edges.append(entry)

    annotation = {
        "type": "prune",
        "text": (
            f"showing {len(nodes)} of {len(nodes) + dropped_nodes} nodes and "
            f"{len(edges)} of {len(edges) + dropped_edges} edges; "
            f"edges with a single co-occurring trial are hidden"
        ),
        "shown_nodes": len(nodes),
        "total_nodes": len(nodes) + dropped_nodes,
        "shown_edges": len(edges),
        "total_edges_before_prune": len(edges) + dropped_edges,
    }

    viz = Visualization(
        type=ChartType.NETWORK_GRAPH,
        title=_network_title(plan, chosen),
        subtitle=_subtitle_from_total(len(studies), ctx.data_timestamp),
        encoding={
            "nodes": {"id": "id", "label": "label"},
            "edges": {"source": "source", "target": "target"},
        },
        data={"nodes": nodes, "edges": edges},
        annotations=[annotation],
    )
    return viz, warnings


def _network_title(plan: AnalysisPlan, pairing: Pairing) -> str:
    subject = plan.filters.intervention or plan.filters.condition or plan.filters.sponsor
    labels = {
        "sponsor_intervention": "Sponsor-Intervention Network",
        "intervention_intervention": "Intervention Co-occurrence Network",
        "condition_intervention": "Condition-Intervention Network",
    }
    head = f"{subject} " if subject else ""
    return f"{head}{labels[pairing]}".strip()


def _subtitle_from_total(total: int, data_timestamp: str) -> str:
    day = data_timestamp.split("T", 1)[0]
    return f"{total:,} studies · ClinicalTrials.gov, data as of {day}"


def _endpoints(study: Mapping[str, Any], pairing: Pairing) -> tuple[list[str], list[str], str, str]:
    sponsor = _one(study, "protocolSection.sponsorCollaboratorsModule.leadSponsor.name")
    interventions = _many(study, "protocolSection.armsInterventionsModule.interventions", "name")
    conditions = _many_list(study, "protocolSection.conditionsModule.conditions")

    if pairing == "sponsor_intervention":
        return (
            [sponsor] if sponsor else [],
            interventions,
            "sponsor",
            "intervention",
        )
    if pairing == "intervention_intervention":
        return interventions, interventions, "intervention", "intervention"
    return conditions, interventions, "condition", "intervention"


def _pairs(left: list[str], right: list[str], pairing: Pairing) -> list[tuple[str, str]]:
    if pairing == "intervention_intervention":
        # Co-occurring distinct interventions within one trial.
        out: list[tuple[str, str]] = []
        for i, a in enumerate(left):
            for b in left[i + 1 :]:
                out.append((a, b) if a <= b else (b, a))
        return out
    return [(a, b) for a in left for b in right if a != b]


def _one(study: Mapping[str, Any], path: str) -> str | None:
    try:
        value = value_at(study, path)
    except KeyError:
        return None
    return str(value) if value else None


def _many(study: Mapping[str, Any], list_path: str, child: str) -> list[str]:
    try:
        items = value_at(study, list_path)
    except KeyError:
        return []
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if isinstance(item, Mapping) and child in item and item[child]:
            out.append(str(item[child]))
    return out


def _many_list(study: Mapping[str, Any], path: str) -> list[str]:
    try:
        items = value_at(study, path)
    except KeyError:
        return []
    if not isinstance(items, list):
        return []
    return [str(item) for item in items if item]


def _safe_nct(study: Mapping[str, Any]) -> str | None:
    try:
        return nct_id_of(study)
    except KeyError:
        return None
