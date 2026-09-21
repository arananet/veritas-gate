"""Repair domain models.

A repair plan is a *contract*: the normalized, machine-readable output of the
RepairPlanner and the only thing a repair agent is given. Raw judge output,
judge reasoning and prior agent reasoning never reach the agent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Priority = Literal["blocking", "recommended", "optional"]

ActionType = Literal[
    "artifact_edit",
    "code_change",
    "documentation",
    "test_change",
    "experiment",
    "dataset_change",
    "claim_change",
    "human_decision",
]

RepairStatus = Literal["completed", "partial", "failed", "human_required"]

# Action types that change what the evidence *is*, rather than how existing
# evidence is represented. These are never autonomous by default.
EVIDENCE_CREATING_ACTIONS: frozenset[str] = frozenset(
    {"experiment", "dataset_change", "claim_change", "human_decision"}
)


class RepairAction(BaseModel):
    """One concrete instruction derived from one or more findings."""

    model_config = ConfigDict(extra="ignore")

    id: str
    finding_ids: list[str] = Field(default_factory=list)
    priority: Priority = "recommended"
    action_type: ActionType = "artifact_edit"
    instruction: str
    rationale: str = ""
    allowed_files: list[str] = Field(default_factory=list)
    available_evidence: list[str] = Field(default_factory=list)
    requires_new_evidence: bool = False
    requires_human_approval: bool = False
    blocked_reason: str | None = None

    @property
    def autonomous(self) -> bool:
        """Whether this action may be dispatched to an agent without a human."""
        return not (self.requires_human_approval or self.requires_new_evidence)


class RepairPlan(BaseModel):
    """The full set of actions proposed for one iteration."""

    model_config = ConfigDict(extra="ignore")

    evaluation_run_id: str
    iteration: int
    actions: list[RepairAction] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def autonomous_actions(self) -> list[RepairAction]:
        return [action for action in self.actions if action.autonomous]

    @property
    def human_actions(self) -> list[RepairAction]:
        return [action for action in self.actions if not action.autonomous]

    @property
    def blocking_human_actions(self) -> list[RepairAction]:
        """Human-only actions that also block the gate — these stop the loop."""
        return [action for action in self.human_actions if action.priority == "blocking"]

    def for_agent(self) -> RepairPlan:
        """The subset an agent is allowed to see: autonomous actions only."""
        return self.model_copy(update={"actions": self.autonomous_actions})


class AppliedChange(BaseModel):
    """A file the repair agent changed."""

    model_config = ConfigDict(extra="ignore")

    file: str
    description: str = ""


class RepairResult(BaseModel):
    """What the agent reports back.

    This is a report, not a verdict. Whether the repair actually worked is
    decided by the next independent evaluation, never by this object.
    """

    model_config = ConfigDict(extra="ignore")

    action_ids: list[str] = Field(default_factory=list)
    status: RepairStatus = "completed"
    changes: list[AppliedChange] = Field(default_factory=list)
    evidence_used: list[str] = Field(default_factory=list)
    new_evidence_created: list[str] = Field(default_factory=list)
    claims_modified: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)

    @property
    def changed_files(self) -> list[str]:
        return [change.file for change in self.changes]
