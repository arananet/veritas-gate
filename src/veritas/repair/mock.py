"""MockRepairAgent.

A deterministic, scriptable agent used by the test suite and by
``--agent mock`` when you want to exercise the loop without a model. It applies
exactly what it is told to apply and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from veritas.artifacts.base import Artifact
from veritas.models.repair import AppliedChange, RepairPlan, RepairResult
from veritas.repair.base import enforce_permissions
from veritas.repair.permissions import RepairPermissions
from veritas.repair.workspace import Workspace

Edit = Callable[[Workspace, RepairPlan], list[AppliedChange]]


class MockRepairAgent:
    """A repair agent whose behaviour is supplied by the caller."""

    name = "mock"

    def __init__(
        self,
        *,
        edits: Edit | None = None,
        status: str = "completed",
        permissions: RepairPermissions | None = None,
        notes: list[str] | None = None,
    ) -> None:
        self.edits = edits
        self.status = status
        self.permissions = permissions or RepairPermissions()
        self.notes = notes or []
        self.seen_plans: list[RepairPlan] = []

    async def repair(
        self,
        artifact: Artifact,
        plan: RepairPlan,
        workspace: Workspace,
    ) -> RepairResult:
        enforce_permissions(plan, self.permissions)
        self.seen_plans.append(plan)

        changes: list[AppliedChange] = []
        if self.edits is not None:
            changes = self.edits(workspace, plan)

        return RepairResult(
            action_ids=[action.id for action in plan.actions],
            status=self.status,  # type: ignore[arg-type]
            changes=changes,
            evidence_used=[item for action in plan.actions for item in action.available_evidence],
            notes=[*self.notes, f"mock agent handled {len(plan.actions)} action(s)"],
            metadata={"agent": self.name},
        )


def write_file(
    workspace: Workspace, relative: str, content: str, description: str
) -> AppliedChange:
    """Helper for tests and scripted runs: write a file inside the workspace."""
    target = Path(workspace.root) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return AppliedChange(file=relative, description=description)
