"""Co-occurrence network graphs. SPEC §5.4.

Available **only** in `complete_records`, where co-occurrence is computed from the full result
set and is therefore exact and unbiased. Sampled co-occurrence is not offered at all: a
network built from a relevance-ranked sample looks authoritative and isn't.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from app.engine.citations import nct_id_of, value_at
from app.engine.context import RunContext
from app.engine.dimensions import canonical_case, case_merge_assumption
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
    edge_ncts: dict[tuple[str, str], list[tuple[str, str]]] = {}
    node_weights: Counter[str] = Counter()
    node_groups: dict[str, str] = {}

    # Node names are free text, and the registry holds "Placebo" and "placebo" as separate
    # strings for the same thing. Drawing both put one drug on the graph as two dots with the
    # trials split between them, which reads as two treatments rather than one.
    fold = _case_map(studies, chosen, ctx)

    for study in studies:
        left, right, group_left, group_right = _endpoints(study, chosen)
        left = list(dict.fromkeys(fold.get(value, value) for value in left))
        right = list(dict.fromkeys(fold.get(value, value) for value in right))
        if not left or not right:
            continue
        nct = _safe_nct(study)
        # A node's weight is the number of contributing trials. In the
        # intervention-intervention pairing `left` and `right` are the *same* list, so counting
        # both sides doubled every weight: a graph where every node claimed twice its trials.
        seen_in_study: set[str] = set()
        for node, group in ((a, group_left) for a in left):
            node_groups[node] = group
            if node not in seen_in_study:
                node_weights[node] += 1
                seen_in_study.add(node)
        for node, group in ((b, group_right) for b in right):
            node_groups.setdefault(node, group)
            if node not in seen_in_study:
                node_weights[node] += 1
                seen_in_study.add(node)

        excerpt = _excerpt_for(left, right, chosen)
        for source, target in _pairs(left, right, chosen):
            key = (source, target) if source <= target else (target, source)
            edge_weights[key] += 1
            if nct:
                edge_ncts.setdefault(key, []).append((nct, excerpt))

    # Drop single-occurrence edges first, then prune nodes by degree.
    kept_edges = {edge: weight for edge, weight in edge_weights.items() if weight >= 2}
    dropped_edges = len(edge_weights) - len(kept_edges)

    degree: Counter[str] = Counter()
    for (a, b), weight in kept_edges.items():
        degree[a] += weight
        degree[b] += weight

    # Every node the graph could have shown, not just those surviving the weight filter
    # otherwise a node appearing solely in single-trial edges vanishes from the denominator too.
    all_nodes = {node for edge in edge_weights for node in edge}

    ranked = sorted(degree, key=lambda node: (-degree[node], node))
    max_nodes = ctx.options.max_buckets
    kept_nodes = set(ranked[:max_nodes])
    dropped_nodes = max(0, len(all_nodes) - len(kept_nodes))

    final_edges = {
        edge: weight
        for edge, weight in kept_edges.items()
        if edge[0] in kept_nodes and edge[1] in kept_nodes
    }
    # Edges lost because an endpoint failed the node cut are dropped just as surely as those
    # below the weight threshold, and a disclosure that omits them reports "1 of 1 edges" while
    # hiding two. "Always truncated *what*, at *what*, *why*" applies to our own pruning first.
    dropped_by_node_cut = len(kept_edges) - len(final_edges)

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
            cited = edge_ncts.get((source, target), [])[:per_datum]
            entry["citations"] = [
                {
                    "nct_id": nct,
                    "field": _PAIRING_FIELDS[chosen],
                    # A verbatim serialization of the two fields whose co-occurrence *is* the
                    # edge (SPEC §4.2 rule 1). Repeating the NCT id as the excerpt, as this
                    # did, cites nothing: it restates the identifier already in `nct_id` and
                    # gives a reader no way to check the claim.
                    "excerpt": excerpt_text,
                    "url": f"https://clinicaltrials.gov/study/{nct}",
                }
                for nct, excerpt_text in cited
            ]
        edges.append(entry)

    annotation = {
        "type": "prune",
        "text": (
            f"showing {len(nodes)} of {len(nodes) + dropped_nodes} nodes and "
            f"{len(edges)} of {len(edge_weights)} edges; "
            f"{dropped_edges} edge(s) hidden as single co-occurrences and "
            f"{dropped_by_node_cut} more dropped with pruned nodes"
        ),
        "shown_nodes": len(nodes),
        "total_nodes": len(nodes) + dropped_nodes,
        "shown_edges": len(edges),
        "total_edges_before_prune": len(edge_weights),
        "edges_below_weight_threshold": dropped_edges,
        "edges_dropped_with_pruned_nodes": dropped_by_node_cut,
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
    # Same rule as encode._title_case: leave an already-capitalised drug name alone, otherwise
    # title-case. A chart headed "glioblastoma Condition-Intervention Network" reads as a bug.
    if subject and not (subject[:1].isupper() and not subject.isupper()):
        subject = subject.title()
    head = f"{subject} " if subject else ""
    return f"{head}{labels[pairing]}".strip()


def _subtitle_from_total(total: int, data_timestamp: str) -> str:
    day = data_timestamp.split("T", 1)[0]
    return f"{total:,} studies · ClinicalTrials.gov, data as of {day}"


def _case_map(
    studies: Sequence[Mapping[str, Any]], pairing: Pairing, ctx: RunContext
) -> dict[str, str]:
    """Value -> display casing across every endpoint this pairing uses."""
    observed: Counter[str] = Counter()
    for study in studies:
        left, right, _, _ = _endpoints(study, pairing)
        observed.update(left)
        if right is not left:
            observed.update(right)

    mapping, merged = canonical_case(observed)
    if not merged:
        return {}

    example = next((display for value, display in mapping.items() if value != display), None)
    ctx.assumptions.append(case_merge_assumption("graph node name(s)", merged, example))
    return mapping


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


_PAIRING_FIELDS: dict[Pairing, str] = {
    "sponsor_intervention": (
        "protocolSection.sponsorCollaboratorsModule.leadSponsor.name + "
        "protocolSection.armsInterventionsModule.interventions[].name"
    ),
    "intervention_intervention": "protocolSection.armsInterventionsModule.interventions[].name",
    "condition_intervention": (
        "protocolSection.conditionsModule.conditions + "
        "protocolSection.armsInterventionsModule.interventions[].name"
    ),
}
"""The structured fields whose co-occurrence constitutes an edge: never a narrative field."""


def _excerpt_for(left: list[str], right: list[str], pairing: Pairing) -> str:
    """Compact JSON of the values that produced the edge, exactly as upstream returned them."""
    if pairing == "intervention_intervention":
        return json.dumps({"interventions": left}, separators=(",", ":"), ensure_ascii=False)
    head = "sponsor" if pairing == "sponsor_intervention" else "conditions"
    return json.dumps(
        {head: left, "interventions": right}, separators=(",", ":"), ensure_ascii=False
    )


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
