"""Build a claim graph from whatever the judges reported."""

from __future__ import annotations

from collections.abc import Iterable

from veritas.claims.graph import ClaimGraph
from veritas.models.evaluation import JudgeResult


def build_graph(judge_results: Iterable[JudgeResult]) -> ClaimGraph:
    """Merge every judge's claims and evidence into one graph."""
    graph = ClaimGraph()
    for result in judge_results:
        for item in result.evidence:
            graph.add_evidence(item)
        for claim in result.claims:
            graph.add_claim(claim)
    graph.resolve()
    return graph
