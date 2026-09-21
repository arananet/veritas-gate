"""Claim graph construction, status resolution and evidence coverage."""

from __future__ import annotations

from veritas.claims import ClaimGraph, build_graph, claim_findings
from veritas.models import Claim, Evidence, JudgeResult


def test_claim_cannot_be_verified_without_evidence() -> None:
    graph = ClaimGraph()
    graph.add_claim(Claim(id="C-1", text="fast", status="verified"))
    graph.resolve()
    assert graph.claims["C-1"].status == "unverified"


def test_refuting_evidence_marks_a_claim_unsupported() -> None:
    graph = ClaimGraph()
    graph.add_evidence(Evidence(id="E-1", description="benchmark disagrees", supports=False))
    graph.add_claim(Claim(id="C-1", text="37% faster", evidence_ids=["E-1"], status="verified"))
    graph.resolve()
    assert graph.claims["C-1"].status == "unsupported"


def test_mixed_evidence_downgrades_to_partially_supported() -> None:
    graph = ClaimGraph()
    graph.add_evidence(Evidence(id="E-1", description="supports"))
    graph.add_evidence(Evidence(id="E-2", description="refutes", supports=False))
    graph.add_claim(Claim(id="C-1", text="faster", evidence_ids=["E-1", "E-2"], status="verified"))
    graph.resolve()
    assert graph.claims["C-1"].status == "partially-supported"


def test_coverage_counts_only_major_claims_in_the_denominator() -> None:
    graph = ClaimGraph()
    graph.add_evidence(Evidence(id="E-1", description="table 4"))
    graph.add_claim(Claim(id="C-1", text="major verified", evidence_ids=["E-1"], status="verified"))
    graph.add_claim(Claim(id="C-2", text="major unsupported", status="unsupported"))
    graph.add_claim(Claim(id="C-3", text="minor", importance="minor", status="unverified"))
    graph.resolve()
    coverage = graph.coverage()
    assert coverage.total == 3
    assert coverage.major == 2
    assert coverage.coverage == 50.0


def test_partial_support_counts_as_half() -> None:
    graph = ClaimGraph()
    graph.add_evidence(Evidence(id="E-1", description="supports"))
    graph.add_evidence(Evidence(id="E-2", description="refutes", supports=False))
    graph.add_claim(Claim(id="C-1", text="claim", evidence_ids=["E-1", "E-2"], status="verified"))
    graph.resolve()
    assert graph.coverage().coverage == 50.0


def test_empty_graph_reports_zero_coverage() -> None:
    assert ClaimGraph().coverage().coverage == 0.0


def test_duplicate_claims_merge_evidence_and_keep_the_worse_status() -> None:
    graph = ClaimGraph()
    graph.add_evidence(Evidence(id="E-1", description="supports"))
    graph.add_claim(Claim(id="C-1", text="claim", evidence_ids=["E-1"], status="verified"))
    graph.add_claim(Claim(id="C-1", text="claim", status="unsupported"))
    assert graph.claims["C-1"].status == "unsupported"
    assert graph.claims["C-1"].evidence_ids == ["E-1"]


def test_unsupported_claims_become_findings() -> None:
    graph = ClaimGraph()
    graph.add_claim(Claim(id="C-1", text="headline result", status="unsupported"))
    graph.add_claim(Claim(id="C-2", text="side note", importance="minor", status="unsupported"))
    findings = claim_findings(graph)
    assert [item.severity for item in findings] == ["major", "minor"]


def test_build_graph_merges_every_judge() -> None:
    results = [
        JudgeResult(
            judge="evidence",
            claims=[Claim(id="C-1", text="a", evidence_ids=["E-1"], status="verified")],
            evidence=[Evidence(id="E-1", description="table 4")],
        ),
        JudgeResult(
            judge="adversarial",
            claims=[Claim(id="C-2", text="b", status="unsupported")],
        ),
    ]
    graph = build_graph(results)
    assert set(graph.claims) == {"C-1", "C-2"}
    assert graph.claims["C-1"].status == "verified"
