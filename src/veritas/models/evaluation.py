"""Results produced by judges, checks, the meta review and the gate."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from veritas.models.claim import Claim, Evidence
from veritas.models.finding import Finding, Severity

JudgeStatus = Literal["pass", "fail", "warning", "error"]
CheckStatus = Literal["pass", "fail", "skipped", "error"]
GateStatus = Literal["PASS", "PASS_WITH_WARNINGS", "REVISE", "FAIL"]

EXIT_CODES: dict[str, int] = {
    "PASS": 0,
    "PASS_WITH_WARNINGS": 1,
    "REVISE": 2,
    "FAIL": 3,
}


class JudgeResult(BaseModel):
    """Structured output of one judge run. Never free prose."""

    model_config = ConfigDict(extra="ignore")

    judge: str
    status: JudgeStatus = "pass"
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckResult(BaseModel):
    """Structured output of one deterministic check."""

    model_config = ConfigDict(extra="ignore")

    check: str
    status: CheckStatus = "pass"
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    duration_seconds: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in ("pass", "skipped")


class ConsolidatedFinding(BaseModel):
    """One issue after duplicate findings from several judges were merged."""

    model_config = ConfigDict(extra="ignore")

    finding: Finding
    reported_by: list[str] = Field(default_factory=list)
    severities: dict[str, Severity] = Field(default_factory=dict)
    consensus: Literal["single-source", "confirmed", "disputed"] = "single-source"
    consensus_confidence: Literal["low", "medium", "high"] = "low"
    duplicate_ids: list[str] = Field(default_factory=list)


class Disagreement(BaseModel):
    """A recorded conflict between judges that must not be averaged away."""

    model_config = ConfigDict(extra="ignore")

    finding_id: str
    title: str
    positions: dict[str, str]
    note: str


class RequiredAction(BaseModel):
    """An action the meta review believes is needed to clear a finding."""

    model_config = ConfigDict(extra="ignore")

    finding: str
    action: str
    files: list[str] = Field(default_factory=list)


class MetaReview(BaseModel):
    """Consolidated view across judges and checks."""

    model_config = ConfigDict(extra="ignore")

    consolidated: list[ConsolidatedFinding] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    required_actions: list[RequiredAction] = Field(default_factory=list)
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def findings(self) -> list[Finding]:
        return [item.finding for item in self.consolidated]


class ClaimCoverage(BaseModel):
    """Evidence coverage over the claim graph. Never a standalone quality score."""

    model_config = ConfigDict(extra="ignore")

    total: int = 0
    major: int = 0
    verified: int = 0
    partially_supported: int = 0
    unsupported: int = 0
    unverified: int = 0
    coverage: float = 0.0


class GateResult(BaseModel):
    """Deterministic gate decision."""

    model_config = ConfigDict(extra="ignore")

    status: GateStatus
    critical: int = 0
    major: int = 0
    minor: int = 0
    info: int = 0
    blocking_findings: list[str] = Field(default_factory=list)
    failed_checks: list[str] = Field(default_factory=list)
    judge_errors: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]


class JudgeStability(BaseModel):
    """Calibration record for a judge run more than once."""

    model_config = ConfigDict(extra="ignore")

    judge: str
    runs: int
    statuses: list[str] = Field(default_factory=list)
    worst_severities: list[Severity] = Field(default_factory=list)
    stability: Literal["HIGH", "MEDIUM", "LOW"] = "HIGH"


class RunManifest(BaseModel):
    """Everything needed to reproduce a run."""

    model_config = ConfigDict(extra="ignore")

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    veritas_version: str
    profile: str
    profile_version: str
    artifact_id: str
    artifact_type: str
    artifact_paths: list[str] = Field(default_factory=list)
    artifact_commit: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    models: dict[str, Any] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    judge_versions: dict[str, str] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)


class EvaluationResult(BaseModel):
    """The complete, persisted outcome of one evaluation run."""

    model_config = ConfigDict(extra="ignore")

    manifest: RunManifest
    judge_results: list[JudgeResult] = Field(default_factory=list)
    check_results: list[CheckResult] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    coverage: ClaimCoverage = Field(default_factory=ClaimCoverage)
    meta_review: MetaReview = Field(default_factory=MetaReview)
    gate: GateResult
    stability: list[JudgeStability] = Field(default_factory=list)
