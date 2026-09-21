"""Loop domain models: deltas, ledger entries, iterations and stop reasons."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from veritas.models.evaluation import GateResult
from veritas.models.repair import RepairPlan, RepairResult


class StopReason(StrEnum):
    """Every loop terminates with exactly one of these. There is no implicit exit."""

    QUALITY_GATE_REACHED = "quality_gate_reached"
    MAX_ITERATIONS = "max_iterations"
    NO_PROGRESS = "no_progress"
    REPEATED_BLOCKER = "repeated_blocker"
    REGRESSION = "regression"
    NEW_CRITICAL_FINDING = "new_critical_finding"
    HUMAN_DECISION_REQUIRED = "human_decision_required"
    COST_LIMIT = "cost_limit"
    CHANGE_LIMIT = "change_limit"
    REPAIR_FAILURE = "repair_failure"
    DRY_RUN = "dry_run"
    NOTHING_TO_REPAIR = "nothing_to_repair"


STOP_REASON_TEXT: dict[StopReason, str] = {
    StopReason.QUALITY_GATE_REACHED: "The quality gate passed.",
    StopReason.MAX_ITERATIONS: "The configured iteration limit was reached.",
    StopReason.NO_PROGRESS: "Repairs stopped reducing blocking findings.",
    StopReason.REPEATED_BLOCKER: "The same blocker survived repeated repair attempts.",
    StopReason.REGRESSION: "An iteration made the artifact worse.",
    StopReason.NEW_CRITICAL_FINDING: "A repair introduced a new critical finding.",
    StopReason.HUMAN_DECISION_REQUIRED: (
        "A remaining blocker cannot be resolved without a human decision."
    ),
    StopReason.COST_LIMIT: "The configured cost limit was reached.",
    StopReason.CHANGE_LIMIT: "The configured limit on changed files was reached.",
    StopReason.REPAIR_FAILURE: "The repair agent could not apply the plan.",
    StopReason.DRY_RUN: "Dry run: the plan was produced but nothing was applied.",
    StopReason.NOTHING_TO_REPAIR: "The gate did not pass but no repairable action was found.",
}

LedgerStatus = Literal[
    "OPEN",
    "RESOLVED",
    "IMPROVED",
    "UNCHANGED",
    "REGRESSED",
    "ACCEPTED_RISK",
    "HUMAN_REVIEW",
]


class LedgerEntry(BaseModel):
    """One logical issue, tracked across iterations under a stable identity."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    category: str
    severity: str
    location: str | None = None
    status: LedgerStatus = "OPEN"
    first_seen_iteration: int = 0
    last_seen_iteration: int = 0
    seen_in_iterations: list[int] = Field(default_factory=list)
    severity_history: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    blocking: bool = False
    notes: list[str] = Field(default_factory=list)


class EvaluationDelta(BaseModel):
    """What changed between two evaluations, by issue rather than by score."""

    model_config = ConfigDict(extra="ignore")

    resolved: list[str] = Field(default_factory=list)
    improved: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    regressed: list[str] = Field(default_factory=list)
    new_findings: list[str] = Field(default_factory=list)
    new_critical: list[str] = Field(default_factory=list)
    blocking_before: int = 0
    blocking_after: int = 0

    @property
    def progressed(self) -> bool:
        """Progress means fewer blocking findings and nothing newly critical.

        Deliberately not an aggregate score: a new critical finding outweighs
        any improvement elsewhere.
        """
        if self.new_critical or self.regressed:
            return False
        return self.blocking_after < self.blocking_before

    @property
    def regression(self) -> bool:
        return bool(self.regressed) or self.blocking_after > self.blocking_before


class IterationRecord(BaseModel):
    """The audit record of one loop iteration."""

    model_config = ConfigDict(extra="ignore")

    iteration: int
    evaluation_run_id: str
    gate: GateResult
    plan: RepairPlan | None = None
    repair: RepairResult | None = None
    delta: EvaluationDelta | None = None
    changed_files: list[str] = Field(default_factory=list)
    patch_written: bool = False
    commit_sha: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class LoopResult(BaseModel):
    """The complete outcome of a bounded loop."""

    model_config = ConfigDict(extra="ignore")

    loop_id: str
    profile: str
    artifact_id: str
    mode: Literal["autopilot", "dry-run"] = "autopilot"
    started_at: datetime
    finished_at: datetime | None = None
    iterations: list[IterationRecord] = Field(default_factory=list)
    stop_reason: StopReason
    stop_detail: str = ""
    initial_gate: GateResult | None = None
    final_gate: GateResult | None = None
    ledger: list[LedgerEntry] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    human_decisions: list[str] = Field(default_factory=list)
    usage: dict[str, object] = Field(default_factory=dict)
    workspace: str | None = None
    original_commit: str | None = None

    @property
    def exit_code(self) -> int:
        """The loop's exit code is the final gate's, so CI can gate on it."""
        if self.final_gate is not None:
            return self.final_gate.exit_code
        return 4
