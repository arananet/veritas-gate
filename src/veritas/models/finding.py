"""Findings: the single currency of the evaluation system.

Judges and deterministic checks both emit ``Finding`` objects, so the gate,
the reports and the CLI never need to know which producer a result came from.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["info", "minor", "major", "critical"]

SEVERITY_ORDER: dict[str, int] = {
    "info": 0,
    "minor": 1,
    "major": 2,
    "critical": 3,
}


def severity_rank(severity: str) -> int:
    """Return a sortable rank for ``severity`` (higher is worse)."""
    return SEVERITY_ORDER[severity]


def max_severity(severities: list[str]) -> Severity:
    """Return the worst severity in ``severities``, defaulting to ``info``."""
    if not severities:
        return "info"
    worst = max(severities, key=severity_rank)
    return worst  # type: ignore[return-value]


class Finding(BaseModel):
    """A single machine-readable problem observed in an artifact."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    severity: Severity
    category: str
    description: str
    location: str | None = None
    evidence: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: str | None = None

    @field_validator("evidence", mode="before")
    @classmethod
    def _coerce_evidence(cls, value: object) -> object:
        """Accept a single evidence string; models often return one instead of a list."""
        if isinstance(value, str):
            return [value]
        if value is None:
            return []
        return value

    @property
    def has_evidence(self) -> bool:
        return any(item.strip() for item in self.evidence)

    def discounted(self, factor: float) -> Finding:
        """Return a copy whose confidence is scaled by ``factor``.

        Used to implement the evidence-first rule: a finding that points at no
        evidence is retained but carries less weight than one that does.
        """
        return self.model_copy(update={"confidence": max(0.0, min(1.0, self.confidence * factor))})
