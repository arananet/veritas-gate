"""Core domain models shared by every profile."""

from veritas.models.claim import Claim, ClaimStatus, Evidence
from veritas.models.evaluation import (
    EXIT_CODES,
    CheckResult,
    CheckStatus,
    ClaimCoverage,
    ConsolidatedFinding,
    Disagreement,
    EvaluationResult,
    GateResult,
    GateStatus,
    JudgeResult,
    JudgeStability,
    JudgeStatus,
    MetaReview,
    RequiredAction,
    RunManifest,
)
from veritas.models.finding import SEVERITY_ORDER, Finding, Severity, max_severity, severity_rank

__all__ = [
    "EXIT_CODES",
    "SEVERITY_ORDER",
    "CheckResult",
    "CheckStatus",
    "Claim",
    "ClaimCoverage",
    "ClaimStatus",
    "ConsolidatedFinding",
    "Disagreement",
    "EvaluationResult",
    "Evidence",
    "Finding",
    "GateResult",
    "GateStatus",
    "JudgeResult",
    "JudgeStability",
    "JudgeStatus",
    "MetaReview",
    "RequiredAction",
    "RunManifest",
    "Severity",
    "max_severity",
    "severity_rank",
]
