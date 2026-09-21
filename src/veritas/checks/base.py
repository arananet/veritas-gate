"""Deterministic checks.

Not everything should be judged by a model. Checks produce the same Finding
objects judges do, so the gate treats both identically.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from veritas.artifacts.base import Artifact
from veritas.models.evaluation import CheckResult
from veritas.models.finding import Finding, Severity


@runtime_checkable
class Check(Protocol):
    name: str

    async def run(self, artifact: Artifact) -> CheckResult: ...


def check_finding(
    check: str,
    index: int,
    *,
    title: str,
    severity: Severity,
    description: str,
    location: str | None = None,
    evidence: list[str] | None = None,
    recommendation: str | None = None,
) -> Finding:
    """Build a finding attributed to a deterministic check (confidence is 1.0)."""
    prefix = "".join(char if char.isalnum() else "-" for char in check.upper()).strip("-")
    return Finding(
        id=f"CHECK-{prefix}-{index:03d}",
        title=title,
        severity=severity,
        category=f"check/{check}",
        description=description,
        location=location,
        evidence=evidence or [],
        recommendation=recommendation,
        confidence=1.0,
        source=f"check:{check}",
    )
