"""Findings: the single currency of the evaluation system.

Judges and deterministic checks both emit ``Finding`` objects, so the gate,
the reports and the CLI never need to know which producer a result came from.
"""

from __future__ import annotations

import hashlib
import re
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


Disposition = Literal["artifact", "configuration", "decision", "declared"]


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
    # Who acts on this: change the work, change veritas.yaml, the author decides,
    # or it is a limitation the work already declares. Defaults to the work, so
    # runs recorded before triage existed still load.
    disposition: Disposition = "artifact"
    # Set when a declared limitation was capped, so the cap is never silent.
    original_severity: Severity | None = None

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


_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "in",
    "is",
    "it",
    "its",
    "no",
    "not",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
}


def issue_key(finding: Finding) -> str:
    """A content-derived identity for a finding, stable across runs.

    The per-run ids judges produce (``EVIDENCE-001``) are positional and change
    between evaluations. This one is derived from the category, the location
    and the significant words of the title, so the same issue reported with
    slightly different wording still maps to the same identity. It is what the
    ledger tracks across iterations, and what a user names to accept a risk.
    """
    words = sorted(word for word in _WORD.findall(finding.title.lower()) if word not in _STOPWORDS)
    material = "|".join([finding.category.lower(), (finding.location or "").strip(), *words])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    return f"F{digest.upper()}"
