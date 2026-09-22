"""LoopOrchestrator: the explicit, bounded state machine.

```text
EVALUATE → gate passed? ── yes ─→ STOP
              │ no
        PLAN REPAIR
              │
        human required? ── yes ─→ STOP
              │ no
           REPAIR
              │
        MEASURE DELTA
              │
        stop condition? ── yes ─→ STOP
              └── no ─→ EVALUATE
```

The orchestrator is the only component that decides whether to continue. The
evaluator does not know a repairer exists; the repairer does not know whether
its work succeeded. Every exit carries an explicit StopReason.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from veritas.artifacts.base import Artifact
from veritas.config import LoopConfig
from veritas.engine import Engine
from veritas.loop.delta import compare_evaluations
from veritas.loop.ledger import FindingLedger
from veritas.loop.storage import LoopStore
from veritas.models.evaluation import EvaluationResult, GateResult
from veritas.models.loop import (
    STOP_REASON_TEXT,
    EvaluationDelta,
    IterationRecord,
    LoopResult,
    StopReason,
)
from veritas.models.repair import RepairAction, RepairPlan, RepairResult
from veritas.repair.base import PermissionViolation, RepairAgent
from veritas.repair.planner import RepairPlanner
from veritas.repair.workspace import Workspace


class Phase(StrEnum):
    """Where the loop is. State is explicit, never implied by control flow."""

    EVALUATE = "evaluate"
    PLAN = "plan"
    REPAIR = "repair"
    MEASURE = "measure"
    STOPPED = "stopped"


LoopProgress = Callable[[str, dict[str, Any]], None]
"""``(event, payload)`` — events: iteration, evaluated, planned, repairing,
repaired, delta, stop."""


def _noop(event: str, payload: dict[str, Any]) -> None:  # pragma: no cover - default
    return None


@dataclass(slots=True)
class LoopOptions:
    """Per-invocation switches for one loop."""

    max_iterations: int | None = None
    dry_run: bool = False
    resume: bool = False
    progress: LoopProgress = _noop
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    extra: dict[str, Any] = field(default_factory=dict)


class LoopOrchestrator:
    """Runs the bounded evaluate → repair → re-evaluate loop."""

    def __init__(
        self,
        engine: Engine,
        planner: RepairPlanner,
        agent: RepairAgent,
        config: LoopConfig,
        store: LoopStore,
        options: LoopOptions | None = None,
    ) -> None:
        self.engine = engine
        self.planner = planner
        self.agent = agent
        self.config = config
        self.store = store
        self.options = options or LoopOptions()
        self.phase = Phase.EVALUATE

    @property
    def max_iterations(self) -> int:
        """There is always a maximum. An unbounded loop is not an option."""
        configured = self.options.max_iterations or self.config.max_iterations
        return max(1, configured)

    async def run(self, artifact: Artifact, workspace: Workspace) -> LoopResult:
        started = self.options.now()
        ledger = (
            FindingLedger.load(self.store.ledger_path()) if self.options.resume else FindingLedger()
        )
        result = LoopResult(
            loop_id=self.store.loop_id,
            profile=self.engine.profile.name,
            artifact_id=artifact.id,
            mode="dry-run" if self.options.dry_run else "autopilot",
            started_at=started,
            stop_reason=StopReason.MAX_ITERATIONS,
            workspace=str(workspace.root),
            original_commit=workspace.original_commit,
        )

        previous: EvaluationResult | None = None
        non_improving = 0
        changed_files: list[str] = []
        stop: StopReason | None = None
        detail = ""

        for iteration in range(1, self.max_iterations + 1):
            self.options.progress(
                "iteration", {"iteration": iteration, "total": self.max_iterations}
            )
            iteration_dir = self.store.iteration_dir(iteration)
            record = IterationRecord(
                iteration=iteration,
                evaluation_run_id="",
                gate=_empty_gate(),
                started_at=self.options.now(),
            )

            # ---------------------------------------------------- EVALUATE
            self.phase = Phase.EVALUATE
            evaluation = await self.engine.run(artifact)
            record.evaluation_run_id = evaluation.manifest.run_id
            record.gate = evaluation.gate
            self.store.write_evaluation(iteration_dir, evaluation)
            self.store.write_findings_before(iteration_dir, evaluation)
            if result.initial_gate is None:
                result.initial_gate = evaluation.gate
            result.final_gate = evaluation.gate
            self.options.progress(
                "evaluated",
                {"iteration": iteration, "gate": evaluation.gate, "result": evaluation},
            )

            # The ledger is updated from the *evaluator's* view, never from the
            # repair agent's claim that something was fixed.
            ledger.record(evaluation, iteration)

            # ------------------------------------------------------- DELTA
            if previous is not None:
                delta = compare_evaluations(previous, evaluation, ledger)
                record.delta = delta
                self.store.write_delta(iteration_dir, delta)
                self.options.progress("delta", {"iteration": iteration, "delta": delta})
                stop, detail = self._delta_stop(delta)
                non_improving = 0 if delta.progressed else non_improving + 1
                if (
                    stop is None
                    and non_improving >= self.config.consecutive_non_improving_iterations
                ):
                    stop = StopReason.NO_PROGRESS
                    detail = (
                        f"{non_improving} consecutive iteration(s) did not reduce blocking "
                        f"findings ({delta.blocking_after} remaining)."
                    )

            # -------------------------------------------------------- GATE
            if evaluation.gate.status == "PASS" or (
                evaluation.gate.status == "PASS_WITH_WARNINGS" and self.config.accept_warnings
            ):
                stop = StopReason.QUALITY_GATE_REACHED
                detail = f"The gate returned {evaluation.gate.status}."

            if stop is not None:
                result.iterations.append(_finish(record, self.options.now()))
                break

            # Repeated blockers are checked against the ledger, which is the
            # only place issue identity survives across evaluations.
            repeated = ledger.repeated_blockers(self.config.same_blocker_repeated)
            if repeated and iteration > 1:
                stop = StopReason.REPEATED_BLOCKER
                detail = (
                    f"{len(repeated)} blocker(s) survived "
                    f"{self.config.same_blocker_repeated} iterations: "
                    + ", ".join(entry.id for entry in repeated)
                )
                result.iterations.append(_finish(record, self.options.now()))
                break

            # -------------------------------------------------------- PLAN
            self.phase = Phase.PLAN
            plan = self.planner.plan(evaluation, iteration, ledger)
            record.plan = plan
            self.store.write_plan(iteration_dir, plan)
            self.options.progress("planned", {"iteration": iteration, "plan": plan})

            human = plan.blocking_human_actions
            if human:
                ledger.mark_human_review(
                    _ledger_ids_for(ledger, evaluation, human),
                    "requires a human decision before it can be repaired",
                )
                result.human_decisions = [action.id for action in human]

            # A human decision stops the loop either way; what the mode decides
            # is whether the mechanical work happens first. Deferring never
            # widens what an agent may touch: an action marked as needing a
            # human is not in `plan.autonomous_actions` and is never dispatched.
            if human and (self.config.on_human_decision == "stop" or not plan.autonomous_actions):
                stop = StopReason.HUMAN_DECISION_REQUIRED
                detail = _human_detail(human)
                result.iterations.append(_finish(record, self.options.now()))
                break

            if not plan.autonomous_actions:
                stop = StopReason.NOTHING_TO_REPAIR
                detail = "No autonomous repair action could be derived from the findings."
                result.iterations.append(_finish(record, self.options.now()))
                break

            if self.options.dry_run:
                stop = StopReason.DRY_RUN
                detail = (
                    f"{len(plan.autonomous_actions)} autonomous action(s) proposed; "
                    "nothing was applied."
                )
                result.iterations.append(_finish(record, self.options.now()))
                break

            # ------------------------------------------------------ REPAIR
            self.phase = Phase.REPAIR
            self.options.progress("repairing", {"iteration": iteration, "plan": plan})
            before = workspace.snapshot()
            repair = await self._repair(artifact, plan, workspace)
            record.repair = repair
            self.store.write_repair(iteration_dir, repair)

            observed = [
                path for path in workspace.changed_since(before) if not path.startswith(".veritas/")
            ]
            record.changed_files = observed
            for path in observed:
                if path not in changed_files:
                    changed_files.append(path)
            record.patch_written = self.store.write_patch(iteration_dir, workspace)
            record.commit_sha = workspace.head_commit()
            self.options.progress(
                "repaired", {"iteration": iteration, "repair": repair, "changed": observed}
            )

            if repair.status == "failed":
                stop = StopReason.REPAIR_FAILURE
                detail = "; ".join(repair.notes) or "The repair agent reported a failure."
                result.iterations.append(_finish(record, self.options.now()))
                break
            if repair.status == "human_required":
                stop = StopReason.HUMAN_DECISION_REQUIRED
                detail = "; ".join(repair.notes) or (
                    "The repair agent reported that a human decision is required."
                )
                result.iterations.append(_finish(record, self.options.now()))
                break
            if repair.new_evidence_created:
                # A repair agent must never be the source of new evidence.
                stop = StopReason.HUMAN_DECISION_REQUIRED
                detail = (
                    "The repair agent reported creating new evidence "
                    f"({', '.join(repair.new_evidence_created)}). Autonomous generation of "
                    "evidence is prohibited; a human must review these changes."
                )
                result.iterations.append(_finish(record, self.options.now()))
                break

            if (
                self.config.max_changed_files is not None
                and len(changed_files) > self.config.max_changed_files
            ):
                stop = StopReason.CHANGE_LIMIT
                detail = (
                    f"{len(changed_files)} files changed, over the configured limit of "
                    f"{self.config.max_changed_files}."
                )
                result.iterations.append(_finish(record, self.options.now()))
                break

            cost = _cost_so_far(result, evaluation)
            if self.config.max_cost_usd is not None and cost > self.config.max_cost_usd:
                stop = StopReason.COST_LIMIT
                detail = f"Estimated cost {cost:.2f} USD exceeds the configured limit."
                result.iterations.append(_finish(record, self.options.now()))
                break

            if human:
                # The mechanical work is done; now ask, against a cleaner artifact.
                # Report the files that actually changed, never the count of
                # actions dispatched: an agent can run and repair nothing.
                stop = StopReason.HUMAN_DECISION_REQUIRED
                detail = _human_detail(human, changed=len(observed))
                result.iterations.append(_finish(record, self.options.now()))
                break

            self.phase = Phase.MEASURE
            previous = evaluation
            result.iterations.append(_finish(record, self.options.now()))
        else:
            stop = StopReason.MAX_ITERATIONS
            detail = f"Reached the configured limit of {self.max_iterations} iterations."

        self.phase = Phase.STOPPED
        result.stop_reason = stop or StopReason.MAX_ITERATIONS
        result.stop_detail = detail or STOP_REASON_TEXT.get(result.stop_reason, "")
        result.finished_at = self.options.now()
        result.ledger = ledger.as_list()
        result.files_changed = changed_files
        result.usage = _aggregate_usage(result)
        ledger.save(self.store.ledger_path())
        self.options.progress("stop", {"result": result})
        return result

    async def _repair(
        self, artifact: Artifact, plan: RepairPlan, workspace: Workspace
    ) -> RepairResult:
        """Dispatch to the agent, converting a refusal into data, not a crash."""
        try:
            return await self.agent.repair(artifact, plan.for_agent(), workspace)
        except PermissionViolation as exc:
            return RepairResult(
                action_ids=[action.id for action in plan.autonomous_actions],
                status="human_required",
                notes=[f"permission violation: {exc}"],
            )
        except Exception as exc:  # an agent is external; its failure is a stop reason
            return RepairResult(
                action_ids=[action.id for action in plan.autonomous_actions],
                status="failed",
                notes=[f"the repair agent raised {type(exc).__name__}: {exc}"],
            )

    def _delta_stop(self, delta: EvaluationDelta) -> tuple[StopReason | None, str]:
        if delta.new_critical and self.config.stop_on_new_critical:
            return (
                StopReason.NEW_CRITICAL_FINDING,
                "A repair introduced new critical finding(s): " + ", ".join(delta.new_critical),
            )
        if delta.regression and self.config.stop_on_regression:
            detail = (
                f"Blocking findings went from {delta.blocking_before} to {delta.blocking_after}."
            )
            if delta.regressed:
                detail += " Regressed: " + ", ".join(delta.regressed) + "."
            return StopReason.REGRESSION, detail
        return None, ""


def _finish(record: IterationRecord, now: datetime) -> IterationRecord:
    record.finished_at = now
    return record


def _empty_gate() -> GateResult:
    """A placeholder gate for an iteration record before its evaluation lands."""
    return GateResult(status="FAIL")


def _human_detail(actions: list[RepairAction], changed: int | None = None) -> str:
    lines = []
    if changed is not None:
        did = f"{changed} file(s) were changed first" if changed else "no file was changed"
        lines.append(f"{did}; {len(actions)} decision(s) remain for a human:")
    for action in actions:
        findings = ", ".join(action.finding_ids)
        reason = action.blocked_reason or "requires a human decision"
        lines.append(f"{action.id} ({findings}): {reason}")
    return " ".join(lines)


def _ledger_ids_for(
    ledger: FindingLedger, evaluation: EvaluationResult, actions: list[RepairAction]
) -> list[str]:
    mapping = ledger.entries_for_findings(evaluation.meta_review.findings)
    ids: list[str] = []
    for action in actions:
        for finding_id in action.finding_ids:
            entry_id = mapping.get(finding_id)
            if entry_id and entry_id not in ids:
                ids.append(entry_id)
    return ids


def _cost_so_far(result: LoopResult, evaluation: EvaluationResult) -> float:
    """Best-effort spend so far.

    Only counted when a provider actually reports a cost; token counts are not
    guessed at a price, so an unset cost never trips the limit spuriously.
    """
    total = 0.0
    for record in result.iterations:
        cost = record.gate.model_extra.get("cost_usd") if record.gate.model_extra else None
        if isinstance(cost, (int, float)):
            total += float(cost)
    current = evaluation.manifest.usage.get("cost_usd")
    if isinstance(current, (int, float)):
        total += float(current)
    return total


def _aggregate_usage(result: LoopResult) -> dict[str, Any]:
    return {
        "iterations": len(result.iterations),
        "files_changed": len(result.files_changed),
        "repairs_applied": sum(1 for item in result.iterations if item.repair is not None),
    }


def loop_paths(root: Path) -> Path:
    return root / ".veritas" / "loops"
