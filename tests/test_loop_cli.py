"""The `veritas loop` command end to end, with a local provider endpoint.

Two scenarios are mandatory, and both are here:

1. evaluate → repair → re-evaluate → delta → repair → deterministic stop
2. a loop that stops with HUMAN_DECISION_REQUIRED because the remaining blocker
   would require experimental evidence that does not exist
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from veritas.cli import app
from veritas.models.loop import StopReason

runner = CliRunner()


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")


def write_project(tmp_path: Path, base_url: str, repo_root: Path, extra: str = "") -> Path:
    (tmp_path / "doc.md").write_text("# Design\n\nThe router is fast.\n")
    (tmp_path / "veritas.yaml").write_text(
        f"""
profile: generic-document
profile_paths:
  - {repo_root / "profiles"}
artifact:
  paths:
    - doc.md
models:
  default:
    provider: openai
    model: test-model
    base_url: {base_url}
    max_retries: 1
gate:
  fail_on:
    - critical
  max_major: 0
workspace:
  mode: current
repair:
  agent:
    provider: mock
loop:
  max_iterations: 4
{extra}
"""
    )
    return tmp_path


REPAIRABLE = {
    "status": "fail",
    "summary": "The document uses two names for one component.",
    "findings": [
        {
            "title": "Terminology is inconsistent",
            "severity": "major",
            "category": "clarity",
            "description": "Two names are used for one component.",
            "location": "doc.md",
            "evidence": ["'router' in one section", "'dispatcher' in another"],
            "recommendation": "Use one name consistently.",
            "confidence": 0.9,
        }
    ],
}

NEEDS_EXPERIMENT = {
    "status": "fail",
    "summary": "The performance claim rests on a single run.",
    "findings": [
        {
            "title": "The primary performance claim rests on one unseeded run",
            "severity": "major",
            "category": "statistics",
            "description": "One run is reported with no variance.",
            "location": "doc.md",
            "evidence": ["the document reports a single aggregate number"],
            "recommendation": "Run the experiment with 30 seeds and report mean and variance.",
            "confidence": 0.95,
        }
    ],
}

CLEAN = {"status": "pass", "summary": "No issues found.", "findings": []}


def sequencing(payloads: list[dict[str, Any]]):
    """Serve one payload per evaluation, three judge calls per evaluation."""
    state = {"calls": 0}

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        index = min(state["calls"] // 3, len(payloads) - 1)
        state["calls"] += 1
        return 200, {"choices": [{"message": {"content": json.dumps(payloads[index])}}]}

    return handler


# ------------------------------------------------------ scenario 1: success


def test_loop_repairs_and_reaches_the_gate(tmp_path: Path, serve, repo_root) -> None:
    with serve(sequencing([REPAIRABLE, CLEAN])) as server:
        project = write_project(tmp_path, server.base_url, repo_root)
        result = runner.invoke(app, ["loop", str(project)])

    assert result.exit_code == 0, result.stdout
    assert "QUALITY_GATE_REACHED" in result.stdout

    loop_dir = _latest_loop(project)
    payload = json.loads((loop_dir / "loop-result.json").read_text())
    assert payload["stop_reason"] == StopReason.QUALITY_GATE_REACHED.value
    assert len(payload["iterations"]) == 2
    assert payload["initial_gate"]["status"] == "REVISE"
    assert payload["final_gate"]["status"] == "PASS"

    # The audit trail: each iteration preserved, nothing overwritten.
    assert (loop_dir / "iteration-001" / "evaluation" / "results.json").is_file()
    assert (loop_dir / "iteration-001" / "repair-plan.json").is_file()
    assert (loop_dir / "iteration-001" / "repair-result.json").is_file()
    assert (loop_dir / "iteration-002" / "delta.json").is_file()
    assert (loop_dir / "loop-report.md").is_file()
    assert (loop_dir / "ledger.json").is_file()

    delta = json.loads((loop_dir / "iteration-002" / "delta.json").read_text())
    assert delta["resolved"], "the delta must record what the re-evaluation resolved"

    report = (loop_dir / "loop-report.md").read_text()
    assert "# Veritas Gate — Loop Report" in report
    assert "## Findings ledger" in report


# --------------------------------------------- scenario 2: human decision


def test_loop_stops_for_a_blocker_that_needs_new_evidence(tmp_path: Path, serve, repo_root) -> None:
    """The safety invariant, end to end through the CLI."""
    with serve(sequencing([NEEDS_EXPERIMENT])) as server:
        project = write_project(tmp_path, server.base_url, repo_root)
        before = (project / "doc.md").read_bytes()
        result = runner.invoke(app, ["loop", str(project)])

    assert "HUMAN_DECISION_REQUIRED" in result.stdout
    assert "Autonomous generation of experimental evidence is prohibited" in result.stdout
    assert "veritas loop . --resume" in result.stdout
    # Nothing was touched, because no autonomous action existed.
    assert (project / "doc.md").read_bytes() == before

    payload = json.loads((_latest_loop(project) / "loop-result.json").read_text())
    assert payload["stop_reason"] == StopReason.HUMAN_DECISION_REQUIRED.value
    assert payload["human_decisions"]
    assert len(payload["iterations"]) == 1

    plan = json.loads((_latest_loop(project) / "iteration-001" / "repair-plan.json").read_text())
    action = plan["actions"][0]
    assert action["requires_new_evidence"] is True
    assert action["requires_human_approval"] is True
    assert action["action_type"] == "experiment"


# ------------------------------------------------------------- other modes


def test_dry_run_changes_nothing(tmp_path: Path, serve, repo_root) -> None:
    with serve(sequencing([REPAIRABLE])) as server:
        project = write_project(tmp_path, server.base_url, repo_root)
        before = {
            path.relative_to(project).as_posix(): path.read_bytes()
            for path in sorted(project.rglob("*"))
            if path.is_file()
        }
        result = runner.invoke(app, ["loop", str(project), "--dry-run"])

    assert "DRY_RUN" in result.stdout
    after = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file() and ".veritas" not in path.parts
    }
    for name, content in after.items():
        assert before[name] == content, f"{name} was modified during a dry run"


def test_max_iterations_option_bounds_the_loop(tmp_path: Path, serve, repo_root) -> None:
    with serve(sequencing([REPAIRABLE])) as server:
        project = write_project(
            tmp_path,
            server.base_url,
            repo_root,
            extra="  consecutive_non_improving_iterations: 99\n  same_blocker_repeated: 99\n",
        )
        result = runner.invoke(app, ["loop", str(project), "--max-iterations", "2"])

    payload = json.loads((_latest_loop(project) / "loop-result.json").read_text())
    assert len(payload["iterations"]) == 2
    assert payload["stop_reason"] in (
        StopReason.MAX_ITERATIONS.value,
        StopReason.NO_PROGRESS.value,
        StopReason.REPEATED_BLOCKER.value,
    )
    assert result.exit_code == 2


def test_assist_mode_writes_a_plan_and_changes_nothing(tmp_path: Path, serve, repo_root) -> None:
    with serve(sequencing([REPAIRABLE])) as server:
        project = write_project(tmp_path, server.base_url, repo_root)
        before = (project / "doc.md").read_bytes()
        result = runner.invoke(app, ["repair-plan", str(project)])

    assert result.exit_code == 0, result.stdout
    assert (project / "doc.md").read_bytes() == before
    plan = json.loads((project / ".veritas" / "repair-plan.json").read_text())
    assert plan["actions"][0]["finding_ids"]
    assert "Assist mode changes nothing" in result.stdout


def test_loop_report_command_reads_the_latest_loop(tmp_path: Path, serve, repo_root) -> None:
    with serve(sequencing([REPAIRABLE, CLEAN])) as server:
        project = write_project(tmp_path, server.base_url, repo_root)
        runner.invoke(app, ["loop", str(project)])

    result = runner.invoke(app, ["loop-report", str(project), "--raw"])
    assert result.exit_code == 0
    assert "Loop Report" in result.stdout


def test_diff_compares_two_loop_iterations(tmp_path: Path, serve, repo_root) -> None:
    with serve(sequencing([REPAIRABLE, CLEAN])) as server:
        project = write_project(tmp_path, server.base_url, repo_root)
        runner.invoke(app, ["loop", str(project)])

    result = runner.invoke(app, ["diff", "1", "2", str(project), "--json"])
    assert result.exit_code == 0, result.stdout
    delta = json.loads(result.stdout)
    assert delta["resolved"]
    assert delta["blocking_after"] < delta["blocking_before"]

    human = runner.invoke(app, ["diff", "1", "2", str(project)])
    assert "Resolved:" in human.stdout


def test_diff_rejects_an_unresolvable_reference(tmp_path: Path, serve, repo_root) -> None:
    with serve(sequencing([CLEAN])) as server:
        project = write_project(tmp_path, server.base_url, repo_root)
        runner.invoke(app, ["evaluate", str(project)])
    result = runner.invoke(app, ["diff", "nope", "also-nope", str(project)])
    assert result.exit_code == 4
    assert "could not resolve" in result.stderr


def test_loop_can_be_disabled_in_configuration(tmp_path: Path, serve, repo_root) -> None:
    with serve(sequencing([CLEAN])) as server:
        project = write_project(tmp_path, server.base_url, repo_root, extra="  enabled: false\n")
        result = runner.invoke(app, ["loop", str(project)])
    assert result.exit_code == 4
    assert "loop.enabled is false" in result.stderr


def _latest_loop(project: Path) -> Path:
    base = project / ".veritas" / "loops"
    return base / (base / "latest").read_text().strip()


def test_the_loop_prints_per_judge_progress_not_silence(tmp_path: Path, serve, repo_root) -> None:
    """Regression: an iteration went silent for however long the judges took,
    indistinguishable from a hang against a real, slow evaluation."""
    with serve(sequencing([CLEAN])) as server:
        project = write_project(tmp_path, server.base_url, repo_root)
        result = runner.invoke(app, ["loop", str(project)])

    assert result.exit_code == 0, result.stdout
    assert "Running judges" in result.stdout
    for judge in ("structure", "evidence", "adversarial"):
        assert judge in result.stdout


def test_an_incomplete_repair_shows_the_agent_reasons() -> None:
    """A column of bare "!" marks was the only account of a partial repair."""
    from rich.console import Console

    from veritas.models.repair import RepairResult
    from veritas.reports.loop import LoopReporter

    console = Console(width=100, record=True)
    reporter = LoopReporter(console)
    reporter._repaired(
        {
            "repair": RepairResult(
                action_ids=["ACTION-001", "ACTION-002"],
                status="partial",
                notes=["Human intervention is required for ACTION-002: no such evidence exists."],
            ),
            "changed": ["paper/manuscript.md"],
        }
    )
    output = console.export_text()
    assert "partial" in output
    assert "no such evidence exists" in output


def test_a_completed_repair_prints_no_reasons() -> None:
    from rich.console import Console

    from veritas.models.repair import RepairResult
    from veritas.reports.loop import LoopReporter

    console = Console(width=100, record=True)
    reporter = LoopReporter(console)
    reporter._repaired(
        {
            "repair": RepairResult(
                action_ids=["ACTION-001"], status="completed", notes=["all done"]
            ),
            "changed": ["doc.md"],
        }
    )
    output = console.export_text()
    assert "all done" not in output
    assert "✓" in output


def _finished(files: list[str]):
    from datetime import UTC, datetime

    from veritas.models.loop import LoopResult, StopReason

    return LoopResult(
        loop_id="l",
        profile="p",
        artifact_id="a",
        mode="autopilot",
        started_at=datetime.now(UTC),
        stop_reason=StopReason.HUMAN_DECISION_REQUIRED,
        stop_detail="stopped.",
        files_changed=files,
        workspace="/tmp/ws",
    )


def test_the_summary_prints_the_commands_to_review_and_apply() -> None:
    """Veritas never writes to your tree, so the commands are what you need."""
    from rich.console import Console

    from veritas.reports.loop import LoopReporter

    console = Console(width=200, record=True)
    LoopReporter(console).summary(_finished(["a.md", "b.md"]))
    out = console.export_text()

    assert "git -C /tmp/ws diff" in out
    assert "git -C /tmp/ws diff | git apply" in out
    # Accepting only part of a repair is the common case, not the exception.
    assert "git -C /tmp/ws diff -- a.md b.md | git apply" in out
    assert "Nothing is applied until you run one of these." in out


def test_a_single_changed_file_offers_no_partial_apply() -> None:
    from rich.console import Console

    from veritas.reports.loop import LoopReporter

    console = Console(width=200, record=True)
    LoopReporter(console).summary(_finished(["only.md"]))
    out = console.export_text()
    assert "Take all of it" in out
    assert "Take only the files you accept" not in out


def test_a_repair_that_changed_nothing_offers_no_apply_commands() -> None:
    from rich.console import Console

    from veritas.reports.loop import LoopReporter

    console = Console(width=200, record=True)
    LoopReporter(console).summary(_finished([]))
    out = console.export_text()
    assert "git apply" not in out
    assert "Workspace:" in out


def test_a_long_workspace_path_is_not_wrapped_mid_command() -> None:
    """A path broken across lines cannot be pasted, which is the whole point."""
    from rich.console import Console

    from veritas.reports.loop import LoopReporter

    result = _finished(["paper/manuscript.md"])
    result.workspace = "/Users/someone/Scripts/a-long-project-name/.veritas/workspaces/loop-x"
    console = Console(width=60, record=True)
    LoopReporter(console).summary(result)
    out = console.export_text()
    assert f"git -C {result.workspace} diff" in out


def test_the_left_out_warning_names_a_bounded_number() -> None:
    from rich.console import Console

    from veritas.reports.loop import LoopReporter

    console = Console(width=200, record=True)
    LoopReporter(console).left_out_warning([f"evidence/f{i}.json" for i in range(20)], limit=5)
    out = console.export_text()
    assert "20 file(s)" in out
    assert "evidence/f0.json" in out
    assert "evidence/f19.json" not in out
    assert "and 15 more" in out


def test_no_left_out_warning_when_nothing_is_left_out() -> None:
    from rich.console import Console

    from veritas.reports.loop import LoopReporter

    console = Console(width=200, record=True)
    LoopReporter(console).left_out_warning([])
    assert console.export_text() == ""


def test_apply_commands_use_a_three_way_apply() -> None:
    from rich.console import Console

    from veritas.reports.loop import LoopReporter

    console = Console(width=300, record=True)
    LoopReporter(console).summary(_finished(["a.md", "b.md"]))
    out = console.export_text()
    assert "git -C /tmp/ws diff | git apply --3way" in out
    assert "Commit your own changes first" in out
