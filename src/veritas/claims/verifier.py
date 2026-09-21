"""Turn unsupported claims into findings so the gate can act on them."""

from __future__ import annotations

from veritas.claims.graph import ClaimGraph
from veritas.models.finding import Finding

UNSUPPORTED_SEVERITY = {"major": "major", "minor": "minor"}


def claim_findings(graph: ClaimGraph) -> list[Finding]:
    """Emit one finding per unsupported claim, attributed to the claim graph."""
    findings: list[Finding] = []
    index = 0
    for claim in graph.claims.values():
        if claim.status != "unsupported":
            continue
        index += 1
        severity = UNSUPPORTED_SEVERITY.get(claim.importance, "minor")
        findings.append(
            Finding(
                id=f"CLAIM-{index:03d}",
                title=f"Unsupported claim: {claim.text[:80]}",
                severity=severity,  # type: ignore[arg-type]
                category="claims/unsupported",
                description=(
                    claim.rationale
                    or "The claim graph found no supporting evidence for this claim."
                ),
                location=claim.source_location,
                evidence=[item.description for item in graph.evidence_for(claim.id)],
                recommendation="Provide evidence for the claim or remove it.",
                confidence=0.9,
                source="claim-graph",
            )
        )
    return findings
