"""Evaluation delta: what actually changed between two independent evaluations.

Progress is judged by issue, not by an aggregate score. A quality number moving
from 81 to 83 means nothing here; a new critical finding outweighs any
improvement elsewhere.
"""

from __future__ import annotations

from veritas.loop.ledger import FindingLedger, issue_key
from veritas.models.evaluation import EvaluationResult
from veritas.models.finding import Finding, severity_rank
from veritas.models.loop import EvaluationDelta


def compare_evaluations(
    previous: EvaluationResult,
    current: EvaluationResult,
    ledger: FindingLedger | None = None,
) -> EvaluationDelta:
    """Compare two evaluations issue by issue."""
    before = _index(previous.meta_review.findings, ledger)
    after = _index(current.meta_review.findings, ledger)

    resolved: list[str] = []
    improved: list[str] = []
    unchanged: list[str] = []
    regressed: list[str] = []

    for key, finding in before.items():
        later = after.get(key)
        if later is None:
            resolved.append(finding.id)
            continue
        before_rank = severity_rank(finding.severity)
        after_rank = severity_rank(later.severity)
        if after_rank < before_rank:
            improved.append(later.id)
        elif after_rank > before_rank:
            regressed.append(later.id)
        else:
            unchanged.append(later.id)

    new_findings = [finding.id for key, finding in after.items() if key not in before]
    new_critical = [
        finding.id
        for key, finding in after.items()
        if key not in before and finding.severity == "critical"
    ]

    return EvaluationDelta(
        resolved=sorted(resolved),
        improved=sorted(improved),
        unchanged=sorted(unchanged),
        regressed=sorted(regressed),
        new_findings=sorted(new_findings),
        new_critical=sorted(new_critical),
        blocking_before=blocking_count(previous),
        blocking_after=blocking_count(current),
    )


def blocking_count(result: EvaluationResult) -> int:
    """How many findings the gate is actually blocking on."""
    return len(result.gate.blocking_findings)


def _index(findings: list[Finding], ledger: FindingLedger | None) -> dict[str, Finding]:
    """Key findings by their stable issue identity."""
    indexed: dict[str, Finding] = {}
    for finding in findings:
        if ledger is not None:
            entry = ledger.match(finding)
            key = entry.id if entry is not None else issue_key(finding)
        else:
            key = issue_key(finding)
        # Keep the worst severity when two findings share an identity.
        existing = indexed.get(key)
        if existing is None or severity_rank(finding.severity) > severity_rank(existing.severity):
            indexed[key] = finding
    return indexed
