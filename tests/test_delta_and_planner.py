"""Evaluation delta and repair planning, including the safety invariant."""

from __future__ import annotations

import pytest
from tests.test_ledger import evaluation, finding

from veritas.config import RepairPermissions
from veritas.loop.delta import compare_evaluations
from veritas.models import Finding
from veritas.repair.planner import RepairPlanner

# --------------------------------------------------------------------- delta


def test_delta_reports_resolution_and_new_findings() -> None:
    before = evaluation([finding("A-1", "Missing variance"), finding("A-2", "Unsupported claim")])
    after = evaluation([finding("B-1", "Unsupported claim"), finding("B-2", "Broken reference")])
    delta = compare_evaluations(before, after)
    assert len(delta.resolved) == 1
    assert len(delta.unchanged) == 1
    assert len(delta.new_findings) == 1


def test_delta_detects_improvement_and_regression() -> None:
    before = evaluation([finding("A-1", "Missing variance", "critical")])
    after = evaluation([finding("B-1", "Missing variance", "minor")])
    assert compare_evaluations(before, after).improved

    worse = compare_evaluations(after, before)
    assert worse.regressed
    assert worse.regression


def test_progress_is_measured_by_blocking_findings_not_by_count() -> None:
    before = evaluation(
        [finding("A-1", "Missing variance"), finding("A-2", "Unsupported claim")],
        blocking=["A-1", "A-2"],
    )
    after = evaluation(
        [
            finding("B-1", "Unsupported claim"),
            finding("B-2", "Terminology drift", "minor"),
            finding("B-3", "Unclear caption", "minor"),
        ],
        blocking=["B-1"],
    )
    delta = compare_evaluations(before, after)
    # More findings overall, but fewer blockers: that is progress.
    assert len(after.meta_review.findings) > len(before.meta_review.findings)
    assert delta.progressed


def test_a_new_critical_finding_outweighs_every_improvement() -> None:
    before = evaluation(
        [finding(f"A-{i}", f"Issue number {i}") for i in range(5)],
        blocking=[f"A-{i}" for i in range(5)],
    )
    after = evaluation([finding("B-1", "Fabricated result", "critical")], blocking=["B-1"])
    delta = compare_evaluations(before, after)
    assert delta.new_critical == ["B-1"]
    assert not delta.progressed


# ------------------------------------------------------------------- planner


def planner(**permissions) -> RepairPlanner:
    return RepairPlanner(RepairPermissions(**permissions))


def test_findings_at_one_location_become_one_action() -> None:
    result = evaluation(
        [
            finding("A-1", "Missing variance", location="paper/main.md#results"),
            finding("A-2", "Missing run count", location="paper/main.md#results"),
        ]
    )
    plan = planner().plan(result, iteration=1)
    assert len(plan.actions) == 1
    assert plan.actions[0].finding_ids == ["A-1", "A-2"]
    assert plan.actions[0].allowed_files == ["paper/main.md"]


def test_blocking_findings_produce_blocking_actions() -> None:
    result = evaluation([finding("A-1", "Missing variance")], blocking=["A-1"])
    action = planner().plan(result, iteration=1).actions[0]
    assert action.priority == "blocking"


def test_representation_work_from_existing_evidence_is_autonomous() -> None:
    """Adding a value the artifact already records is not fabrication."""
    item = Finding(
        id="A-1",
        title="Run count is not stated in the manuscript",
        severity="major",
        category="reproducibility",
        description="The results section omits the run count.",
        location="paper/results.md",
        evidence=["results/run_summary.json records N=10, mean=0.873, std=0.004"],
        recommendation=(
            "State the run count and standard deviation already recorded in "
            "results/run_summary.json."
        ),
    )
    action = planner().plan(evaluation([item]), iteration=1).actions[0]
    assert action.autonomous
    assert not action.requires_new_evidence
    assert "results/run_summary.json records N=10" in action.instruction


def test_an_action_needing_new_experiments_is_never_autonomous() -> None:
    """The safety invariant: Veritas will not fabricate missing evidence."""
    item = Finding(
        id="A-1",
        title="Improvement rests on a single run",
        severity="major",
        category="statistics",
        description="One run is reported with no variance.",
        location="paper/results.md",
        evidence=["results/benchmark.json: runs = 1"],
        recommendation="Run the experiment with 30 seeds and report mean and standard deviation.",
    )
    action = planner().plan(evaluation([item]), iteration=1).actions[0]
    assert action.requires_new_evidence
    assert action.requires_human_approval
    assert not action.autonomous
    assert action.action_type == "experiment"
    assert "Do not invent" in action.instruction


def test_a_requested_count_absent_from_the_evidence_requires_a_human() -> None:
    item = Finding(
        id="A-1",
        title="Insufficient repetitions",
        severity="major",
        category="reproducibility",
        description="Results are reported over 3 folds.",
        location="paper/results.md",
        evidence=["results/summary.json records 3 folds"],
        recommendation="Report results using 10 folds.",
    )
    action = planner().plan(evaluation([item]), iteration=1).actions[0]
    assert action.requires_new_evidence


def test_an_evidence_shaped_finding_citing_nothing_requires_a_human() -> None:
    item = Finding(
        id="A-1",
        title="No baseline comparison",
        severity="major",
        category="experimental-design",
        description="No baseline is evaluated.",
        location="paper/main.md",
        evidence=[],
    )
    action = planner().plan(evaluation([item]), iteration=1).actions[0]
    assert action.requires_new_evidence


def test_impermissible_action_types_are_marked_human_only() -> None:
    item = Finding(
        id="A-1",
        title="Test suite does not cover the described behaviour",
        severity="major",
        category="test-coverage",
        description="No test covers the routing path.",
        location="tests/test_router.py",
        evidence=["tests/test_router.py contains no routing test"],
        recommendation="Add a test covering the routing path.",
    )
    allowed = planner(tests=True).plan(evaluation([item]), iteration=1).actions[0]
    assert allowed.autonomous
    assert allowed.action_type == "test_change"

    refused = planner(tests=False).plan(evaluation([item]), iteration=1).actions[0]
    assert not refused.autonomous
    assert "repair.permissions.tests" in (refused.blocked_reason or "")


def test_dataset_and_methodology_work_is_not_autonomous_by_default() -> None:
    item = Finding(
        id="A-1",
        title="Dataset split is not stratified",
        severity="major",
        category="dataset",
        description="The split is random rather than stratified.",
        location="data/split.py",
        evidence=["data/split.py uses random_split"],
        recommendation="Stratify the split.",
    )
    action = planner().plan(evaluation([item]), iteration=1).actions[0]
    assert not action.autonomous


def test_the_plan_given_to_an_agent_contains_only_autonomous_actions() -> None:
    safe = Finding(
        id="A-1",
        title="Terminology is inconsistent",
        severity="minor",
        category="clarity",
        description="Two names for one component.",
        location="paper/main.md",
        evidence=["'router' in Method", "'dispatcher' in Results"],
        recommendation="Use one name consistently.",
    )
    unsafe = Finding(
        id="A-2",
        title="Single run",
        severity="major",
        category="statistics",
        description="One run reported.",
        location="results/benchmark.json",
        evidence=["runs = 1"],
        recommendation="Re-run with multiple seeds.",
    )
    plan = planner().plan(evaluation([safe, unsafe]), iteration=1)
    assert len(plan.actions) == 2
    for_agent = plan.for_agent()
    assert len(for_agent.actions) == 1
    assert for_agent.actions[0].finding_ids == ["A-1"]
    assert plan.blocking_human_actions


def test_the_plan_never_carries_judge_prose_verbatim_as_the_instruction() -> None:
    """The agent receives a contract, not the judge's output."""
    item = Finding(
        id="A-1",
        title="Unclear caption",
        severity="minor",
        category="clarity",
        description="The caption does not name the unit.",
        location="paper/main.md",
        evidence=["Table 1 header reads 'latency'"],
        recommendation="State the unit in the caption.",
    )
    action = planner().plan(evaluation([item]), iteration=1).actions[0]
    assert action.instruction.startswith("Problem:")
    assert "Required action:" in action.instruction
    assert "do not invent" in action.instruction.lower()


def test_permission_reason_names_the_setting_to_change() -> None:
    permissions = RepairPermissions()
    assert "repair.permissions.experiments" in permissions.reason_for("experiment")
    with pytest.raises(AttributeError):
        _ = permissions.no_such_permission_attribute
