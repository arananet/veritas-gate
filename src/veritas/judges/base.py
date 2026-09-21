"""Judge abstraction.

Phase one is blind: a judge sees the artifact and nothing else. It never sees
generator prompts, generator reasoning, other judges' conclusions, or previous
repair attempts. Only the MetaJudge is given multiple judge results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from veritas.artifacts.base import Artifact
from veritas.models.evaluation import JudgeResult


@dataclass(slots=True)
class EvaluationContext:
    """What a judge is allowed to know about the run."""

    profile: str
    profile_version: str
    run_id: str
    rubric: dict[str, Any] = field(default_factory=dict)
    artifact_type: str = "document"
    notes: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Judge(Protocol):
    """Anything that can evaluate an artifact into structured findings."""

    name: str
    version: str

    async def evaluate(self, artifact: Artifact, context: EvaluationContext) -> JudgeResult: ...


def error_result(judge: str, message: str, *, category: str = "judge-error") -> JudgeResult:
    """Represent a judge failure as data so one failure cannot abort the run."""
    from veritas.models.finding import Finding

    return JudgeResult(
        judge=judge,
        status="error",
        summary=message,
        findings=[
            Finding(
                id=f"{judge.upper().replace('-', '_')}-ERROR",
                title=f"Judge '{judge}' could not complete",
                severity="info",
                category=category,
                description=message,
                evidence=[],
                confidence=1.0,
                source=judge,
            )
        ],
        metadata={"error": message},
    )
