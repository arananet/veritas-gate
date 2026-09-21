"""RepairAgent abstraction.

An agent applies authorized changes. It does not decide whether its own work
was correct: that judgement belongs to the next independent evaluation, and the
decision to continue belongs to the LoopOrchestrator.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from veritas.artifacts.base import Artifact
from veritas.models.repair import RepairPlan, RepairResult
from veritas.repair.permissions import RepairPermissions
from veritas.repair.workspace import Workspace


@runtime_checkable
class RepairAgent(Protocol):
    """Anything that can apply a repair plan to a workspace."""

    name: str

    async def repair(
        self,
        artifact: Artifact,
        plan: RepairPlan,
        workspace: Workspace,
    ) -> RepairResult: ...


class PermissionViolation(RuntimeError):
    """Raised when an agent is handed work its permissions do not cover."""


def enforce_permissions(plan: RepairPlan, permissions: RepairPermissions) -> None:
    """Refuse a plan containing anything the permissions do not allow.

    The planner already marks impermissible actions as human-only; this is the
    second gate, so a bug or a hand-edited plan still cannot slip past.
    """
    for action in plan.actions:
        # Checked first because it carries the most informative message: it is
        # the safety invariant, not an ordinary permission setting.
        if action.requires_new_evidence:
            raise PermissionViolation(
                f"{action.id} requires evidence that does not exist; "
                "Veritas does not fabricate evidence to satisfy its own evaluator"
            )
        if not permissions.allows(action.action_type):
            raise PermissionViolation(f"{action.id}: {permissions.reason_for(action.action_type)}")
        if not action.autonomous:
            raise PermissionViolation(
                f"{action.id} is not autonomous and must never reach a repair agent"
            )
