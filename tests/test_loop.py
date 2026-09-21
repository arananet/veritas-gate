"""LoopOrchestrator: every stop condition, and the safety invariant."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.test_ledger import evaluation

from veritas.artifacts import Artifact
from veritas.config import LoopConfig, RepairPermissions
from veritas.loop import LoopOptions, LoopOrchestrator, LoopStore, Phase
from veritas.models.loop import StopReason
from veritas.models.repair import AppliedChange, RepairPlan
from veritas.repair import MockRepairAgent, RepairPlanner
from veritas.repair.mock import write_file
from veritas.repair.workspace import Workspace


class ScriptedEngine:
    """An evaluation engine that replays a fixed sequence of results.

    The loop under test must work with any engine that produces evaluations;
    scripting them is how each stop condition is reached deterministically.
    """

    def __init__(self, results: list[Any], profile_name: str = "generic-document") -> None:
        self.results = list(results)
        self.calls = 0

        class _Profile:
            name = profile_name

        self.profile = _Profile()

    async def run(self, artifact: Artifact) -> Any:
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


def passing() -> Any:
    return evaluation([])


def revise(findings: list[Any], blocking: list[str] | None = None) -> Any:
    result = evaluation(findings, blocking)
    result.gate.status = "REVISE"
    return result


def make_loop(
    tmp_path: Path,
    results: list[Any],
    *,
    agent: Any = None,
    config: LoopConfig | None = None,
    options: LoopOptions | None = None,
) -> tuple[LoopOrchestrator, Workspace, Artifact]:
    workspace_root = tmp_path / "ws"
    workspace_root.mkdir(exist_ok=True)
    (workspace_root / "doc.md").write_text("# Doc\n")
    workspace = Workspace(root=workspace_root, mode="copy")
    artifact = Artifact(id="doc", type="document", root=workspace_root, paths=["doc.md"])
    store = LoopStore(tmp_path / "loops", "loop-test")
    orchestrator = LoopOrchestrator(
        ScriptedEngine(results),  # type: ignore[arg-type]
        RepairPlanner(RepairPermissions()),
        agent or MockRepairAgent(),
        config or LoopConfig(),
        store,
        options or LoopOptions(),
    )
    return orchestrator, workspace, artifact


def repairable(fid: str = "A-1", title: str = "Terminology is inconsistent") -> Any:
    """A finding whose fix is representation work: autonomously repairable."""
    from veritas.models import Finding

    return Finding(
        id=fid,
        title=title,
        severity="major",
        category="clarity",
        description="Two names are used for one component.",
        location="doc.md",
        evidence=["'router' in one section", "'dispatcher' in another"],
        recommendation="Use one name consistently.",
    )


def needs_experiment(fid: str = "E-1") -> Any:
    from veritas.models import Finding

    return Finding(
        id=fid,
        title="Improvement rests on a single run",
        severity="major",
        category="statistics",
        description="One run is reported with no variance.",
        location="results/benchmark.json",
        evidence=["results/benchmark.json: runs = 1"],
        recommendation="Run the experiment with 30 seeds and report the variance.",
    )


# ------------------------------------------------------------ stop reasons


async def test_a_passing_gate_stops_immediately_without_repairing(tmp_path: Path) -> None:
    agent = MockRepairAgent()
    orchestrator, workspace, artifact = make_loop(tmp_path, [passing()], agent=agent)
    result = await orchestrator.run(artifact, workspace)

    assert result.stop_reason is StopReason.QUALITY_GATE_REACHED
    assert len(result.iterations) == 1
    assert orchestrator.engine.calls == 1  # type: ignore[attr-defined]
    assert agent.seen_plans == [], "a passing artifact must never be handed to a repairer"
    assert orchestrator.phase is Phase.STOPPED


async def test_repair_resolves_the_blocker_and_the_gate_passes(tmp_path: Path) -> None:
    def edits(workspace: Workspace, plan: RepairPlan) -> list[AppliedChange]:
        return [write_file(workspace, "doc.md", "# Doc\n\nRouter everywhere.\n", "unified naming")]

    agent = MockRepairAgent(edits=edits)
    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([repairable()]), passing()], agent=agent
    )
    result = await orchestrator.run(artifact, workspace)

    assert result.stop_reason is StopReason.QUALITY_GATE_REACHED
    assert len(result.iterations) == 2
    assert len(agent.seen_plans) == 1
    assert result.files_changed == ["doc.md"]
    assert result.iterations[1].delta is not None
    assert result.iterations[1].delta.resolved == ["A-1"]
    assert result.initial_gate.status == "REVISE"
    assert result.final_gate.status == "PASS"


async def test_max_iterations_bounds_the_loop(tmp_path: Path) -> None:
    orchestrator, workspace, artifact = make_loop(
        tmp_path,
        [revise([repairable(f"A-{i}", f"Issue number {i} in naming")]) for i in range(1, 12)],
        config=LoopConfig(
            max_iterations=3, consecutive_non_improving_iterations=99, same_blocker_repeated=99
        ),
    )
    result = await orchestrator.run(artifact, workspace)

    assert result.stop_reason is StopReason.MAX_ITERATIONS
    assert len(result.iterations) == 3
    assert orchestrator.engine.calls == 3  # type: ignore[attr-defined]


async def test_no_progress_stops_the_loop(tmp_path: Path) -> None:
    """Two iterations that do not reduce blockers end the loop."""
    stuck = revise([repairable()])
    orchestrator, workspace, artifact = make_loop(
        tmp_path,
        [stuck, stuck, stuck, stuck],
        config=LoopConfig(max_iterations=5, same_blocker_repeated=99),
    )
    result = await orchestrator.run(artifact, workspace)
    assert result.stop_reason is StopReason.NO_PROGRESS


async def test_regression_stops_the_loop(tmp_path: Path) -> None:
    before = revise([repairable()], blocking=["A-1"])
    worse = revise(
        [repairable(), repairable("A-2", "A second naming problem appeared")],
        blocking=["A-1", "A-2"],
    )
    orchestrator, workspace, artifact = make_loop(tmp_path, [before, worse])
    result = await orchestrator.run(artifact, workspace)
    assert result.stop_reason is StopReason.REGRESSION
    assert "1 to 2" in result.stop_detail


async def test_a_new_critical_finding_stops_the_loop(tmp_path: Path) -> None:
    from veritas.models import Finding

    critical = Finding(
        id="C-1",
        title="The repair introduced a fabricated result",
        severity="critical",
        category="integrity",
        description="A number appeared that no artifact supports.",
        location="doc.md",
        evidence=["doc.md now claims 0.99 accuracy"],
    )
    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([repairable()]), revise([critical], blocking=["C-1"])]
    )
    result = await orchestrator.run(artifact, workspace)
    assert result.stop_reason is StopReason.NEW_CRITICAL_FINDING
    assert "C-1" in result.stop_detail


async def test_a_repeated_blocker_stops_the_loop(tmp_path: Path) -> None:
    """The same issue surviving repair is a different stop reason from no progress."""
    first = revise(
        [repairable("A-1"), repairable("A-2", "A second naming problem")], blocking=["A-1", "A-2"]
    )
    second = revise([repairable("B-1")], blocking=["B-1"])
    orchestrator, workspace, artifact = make_loop(
        tmp_path,
        [first, second, second],
        config=LoopConfig(
            max_iterations=5, same_blocker_repeated=2, consecutive_non_improving_iterations=99
        ),
    )
    result = await orchestrator.run(artifact, workspace)
    assert result.stop_reason is StopReason.REPEATED_BLOCKER


async def test_a_human_only_blocker_stops_before_any_repair(tmp_path: Path) -> None:
    """The safety invariant, end to end: no agent is invoked at all."""
    agent = MockRepairAgent()
    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([needs_experiment()], blocking=["E-1"])], agent=agent
    )
    result = await orchestrator.run(artifact, workspace)

    assert result.stop_reason is StopReason.HUMAN_DECISION_REQUIRED
    assert agent.seen_plans == []
    assert result.human_decisions
    assert "does not exist" in result.stop_detail
    entry = next(item for item in result.ledger if item.status == "HUMAN_REVIEW")
    assert entry.blocking


async def test_repair_failure_stops_the_loop(tmp_path: Path) -> None:
    agent = MockRepairAgent(status="failed", notes=["the agent could not apply the plan"])
    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([repairable()]), revise([repairable()])], agent=agent
    )
    result = await orchestrator.run(artifact, workspace)
    assert result.stop_reason is StopReason.REPAIR_FAILURE
    assert "could not apply" in result.stop_detail


async def test_an_agent_reporting_new_evidence_stops_the_loop(tmp_path: Path) -> None:
    """Even if an agent fabricates evidence, the loop refuses to continue on it."""

    class FabricatingAgent(MockRepairAgent):
        async def repair(self, artifact, plan, workspace):  # type: ignore[override]
            result = await super().repair(artifact, plan, workspace)
            result.new_evidence_created = ["results/benchmark-30-seeds.json"]
            return result

    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([repairable()]), passing()], agent=FabricatingAgent()
    )
    result = await orchestrator.run(artifact, workspace)
    assert result.stop_reason is StopReason.HUMAN_DECISION_REQUIRED
    assert "prohibited" in result.stop_detail


async def test_a_permission_violation_is_reported_not_raised(tmp_path: Path) -> None:
    agent = MockRepairAgent(permissions=RepairPermissions(documentation=False, source_code=False))
    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([repairable()]), revise([repairable()])], agent=agent
    )
    result = await orchestrator.run(artifact, workspace)
    assert result.stop_reason is StopReason.HUMAN_DECISION_REQUIRED
    assert "permission violation" in result.stop_detail


async def test_the_change_limit_stops_the_loop(tmp_path: Path) -> None:
    def edits(workspace: Workspace, plan: RepairPlan) -> list[AppliedChange]:
        return [
            write_file(workspace, f"extra-{index}.md", f"# {index}\n", "added")
            for index in range(4)
        ]

    orchestrator, workspace, artifact = make_loop(
        tmp_path,
        [revise([repairable()]), revise([repairable()])],
        agent=MockRepairAgent(edits=edits),
        config=LoopConfig(max_changed_files=2, consecutive_non_improving_iterations=99),
    )
    result = await orchestrator.run(artifact, workspace)
    assert result.stop_reason is StopReason.CHANGE_LIMIT


async def test_dry_run_plans_but_changes_nothing(tmp_path: Path) -> None:
    def edits(workspace: Workspace, plan: RepairPlan) -> list[AppliedChange]:
        raise AssertionError("a dry run must never invoke the repair agent")

    agent = MockRepairAgent(edits=edits)
    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([repairable()])], agent=agent, options=LoopOptions(dry_run=True)
    )
    before = (workspace.root / "doc.md").read_bytes()
    result = await orchestrator.run(artifact, workspace)

    assert result.stop_reason is StopReason.DRY_RUN
    assert result.mode == "dry-run"
    assert agent.seen_plans == []
    assert (workspace.root / "doc.md").read_bytes() == before
    assert result.iterations[0].plan is not None


async def test_nothing_to_repair_when_no_action_can_be_derived(tmp_path: Path) -> None:
    result_with_no_findings = evaluation([])
    result_with_no_findings.gate.status = "REVISE"
    orchestrator, workspace, artifact = make_loop(tmp_path, [result_with_no_findings])
    result = await orchestrator.run(artifact, workspace)
    assert result.stop_reason is StopReason.NOTHING_TO_REPAIR


# ------------------------------------------------------------- persistence


async def test_every_iteration_is_preserved_on_disk(tmp_path: Path) -> None:
    def edits(workspace: Workspace, plan: RepairPlan) -> list[AppliedChange]:
        return [write_file(workspace, "doc.md", "# Doc\n\nfixed\n", "fixed")]

    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([repairable()]), passing()], agent=MockRepairAgent(edits=edits)
    )
    result = await orchestrator.run(artifact, workspace)

    root = tmp_path / "loops" / "loop-test"
    first = root / "iteration-001"
    assert (first / "evaluation" / "results.json").is_file()
    assert (first / "findings-before.json").is_file()
    assert (first / "repair-plan.json").is_file()
    assert (first / "repair-result.json").is_file()

    second = root / "iteration-002"
    assert (second / "delta.json").is_file()
    delta = json.loads((second / "delta.json").read_text())
    assert delta["resolved"] == ["A-1"]

    assert (root / "ledger.json").is_file()
    assert result.stop_reason is StopReason.QUALITY_GATE_REACHED


async def test_a_resumed_loop_continues_the_previous_ledger(tmp_path: Path) -> None:
    store = LoopStore(tmp_path / "loops", "loop-test")
    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([needs_experiment()], blocking=["E-1"])]
    )
    first = await orchestrator.run(artifact, workspace)
    assert first.stop_reason is StopReason.HUMAN_DECISION_REQUIRED
    assert store.ledger_path().is_file()

    resumed, workspace2, artifact2 = make_loop(
        tmp_path, [passing()], options=LoopOptions(resume=True)
    )
    second = await resumed.run(artifact2, workspace2)
    assert second.stop_reason is StopReason.QUALITY_GATE_REACHED
    # The earlier issue is carried forward and marked resolved, not forgotten.
    assert any(entry.status == "RESOLVED" for entry in second.ledger)


async def test_the_evaluator_never_receives_the_repair_agents_claims(tmp_path: Path) -> None:
    """Separation of evaluator and repairer, asserted on what the engine sees."""
    seen: list[Artifact] = []

    class RecordingEngine(ScriptedEngine):
        async def run(self, artifact: Artifact) -> Any:
            seen.append(artifact)
            return await super().run(artifact)

    def edits(workspace: Workspace, plan: RepairPlan) -> list[AppliedChange]:
        return [write_file(workspace, "doc.md", "# Doc\n\nfixed\n", "I definitely fixed A-1")]

    workspace_root = tmp_path / "ws"
    workspace_root.mkdir()
    (workspace_root / "doc.md").write_text("# Doc\n")
    workspace = Workspace(root=workspace_root, mode="copy")
    artifact = Artifact(id="doc", type="document", root=workspace_root, paths=["doc.md"])

    orchestrator = LoopOrchestrator(
        RecordingEngine([revise([repairable()]), passing()]),  # type: ignore[arg-type]
        RepairPlanner(RepairPermissions()),
        MockRepairAgent(edits=edits),
        LoopConfig(),
        LoopStore(tmp_path / "loops", "loop-test"),
    )
    await orchestrator.run(artifact, workspace)

    assert len(seen) == 2
    for item in seen:
        payload = json.dumps(item.metadata)
        assert "repair" not in payload.lower()
        assert "ACTION-" not in payload
        # The engine is handed the artifact and nothing else about the repair.
        assert set(item.metadata) == set()


@pytest.mark.parametrize("reason", list(StopReason))
def test_every_stop_reason_has_human_readable_text(reason: StopReason) -> None:
    from veritas.models.loop import STOP_REASON_TEXT

    assert STOP_REASON_TEXT[reason].strip()
