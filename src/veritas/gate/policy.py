"""Gate policy helpers."""

from __future__ import annotations

from veritas.config import GatePolicy
from veritas.models.finding import Finding, severity_rank


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    counts = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def limit_for(policy: GatePolicy, severity: str) -> int | None:
    """Return the configured maximum count for ``severity``."""
    return {
        "critical": policy.max_critical,
        "major": policy.max_major,
        "minor": policy.max_minor,
        "info": None,
    }.get(severity)


def fails_policy(policy: GatePolicy, severity: str) -> bool:
    return severity in policy.fail_on


def revises_policy(policy: GatePolicy, severity: str) -> bool:
    if severity in policy.revise_on:
        return True
    limit = limit_for(policy, severity)
    return limit is not None and severity_rank(severity) >= severity_rank("major")
