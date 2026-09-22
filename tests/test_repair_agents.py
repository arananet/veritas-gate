"""Workspaces and repair agents."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from veritas.artifacts import Artifact
from veritas.config import RepairAgentConfig, RepairConfig, RepairPermissions
from veritas.models.repair import RepairAction, RepairPlan
from veritas.repair import (
    GenericCLIRepairAgent,
    MockRepairAgent,
    PermissionViolation,
    build_repair_agent,
    enforce_permissions,
    open_workspace,
    temporary_workspace,
)
from veritas.repair.workspace import git_available


def plan_with(**overrides) -> RepairPlan:
    action = RepairAction(
        id="ACTION-001",
        finding_ids=["A-1"],
        priority="blocking",
        action_type=overrides.pop("action_type", "artifact_edit"),
        instruction="Problem: the caption omits the unit.\n\nRequired action: state it.",
        allowed_files=["doc.md"],
        available_evidence=["Table 1 header reads 'latency'"],
        **overrides,
    )
    return RepairPlan(evaluation_run_id="r", iteration=1, actions=[action])


# ---------------------------------------------------------------- workspace


def test_copy_workspace_leaves_the_original_untouched(tmp_path: Path) -> None:
    source = tmp_path / "artifact"
    source.mkdir()
    (source / "doc.md").write_text("original\n")

    workspace = open_workspace(source, "copy", loop_id="l1", base_dir=tmp_path / "ws")
    assert workspace.root != source
    (workspace.root / "doc.md").write_text("changed\n")
    assert (source / "doc.md").read_text() == "original\n"


def test_current_workspace_is_the_artifact_itself(tmp_path: Path) -> None:
    source = tmp_path / "artifact"
    source.mkdir()
    (source / "doc.md").write_text("original\n")
    workspace = open_workspace(source, "current")
    assert workspace.root == source.resolve()


def test_changed_files_are_detected_without_git(tmp_path: Path) -> None:
    source = tmp_path / "artifact"
    source.mkdir()
    (source / "doc.md").write_text("one\n")
    (source / "keep.md").write_text("unchanged\n")

    workspace = temporary_workspace(source)
    before = workspace.snapshot()
    (workspace.root / "doc.md").write_text("two\n")
    (workspace.root / "new.md").write_text("added\n")

    changed = workspace.changed_since(before)
    assert "doc.md" in changed
    assert "new.md" in changed
    assert "keep.md" not in changed
    workspace.cleanup()


def test_a_missing_git_never_breaks_a_workspace(tmp_path: Path) -> None:
    source = tmp_path / "artifact"
    source.mkdir()
    (source / "doc.md").write_text("x\n")
    workspace = open_workspace(source, "copy", loop_id="l1", base_dir=tmp_path / "ws")
    assert workspace.original_commit is None
    assert workspace.diff() == ""
    assert workspace.head_commit() is None


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_git_workspace_records_a_commit_and_a_patch(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    (source / "doc.md").write_text("original\n")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-qm", "initial"],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)

    workspace = open_workspace(source, "worktree", loop_id="l1", base_dir=tmp_path / "ws")
    assert workspace.original_commit
    assert workspace.mode in ("worktree", "copy")

    (workspace.root / "doc.md").write_text("changed\n")
    if workspace.mode == "worktree":
        patch = workspace.diff()
        assert "changed" in patch
        assert (source / "doc.md").read_text() == "original\n"
    workspace.cleanup()


# ------------------------------------------------------------------- agents


def test_build_repair_agent_selects_the_provider() -> None:
    assert isinstance(build_repair_agent(RepairConfig()), MockRepairAgent)
    cli = build_repair_agent(
        RepairConfig(agent=RepairAgentConfig(provider="generic-cli", command=["true"]))
    )
    assert isinstance(cli, GenericCLIRepairAgent)


def test_unknown_repair_provider_is_rejected() -> None:
    from veritas.config import ConfigError

    with pytest.raises(ConfigError, match="unknown repair agent provider"):
        build_repair_agent(RepairConfig(agent=RepairAgentConfig(provider="telepathy")))


def test_enforce_permissions_refuses_a_non_autonomous_action() -> None:
    plan = plan_with(requires_human_approval=True)
    with pytest.raises(PermissionViolation, match="not autonomous"):
        enforce_permissions(plan, RepairPermissions())


def test_enforce_permissions_refuses_an_evidence_creating_action() -> None:
    plan = plan_with(requires_new_evidence=True)
    with pytest.raises(PermissionViolation, match="does not fabricate evidence"):
        enforce_permissions(plan, RepairPermissions())


def test_enforce_permissions_refuses_a_disabled_action_type() -> None:
    plan = plan_with(action_type="experiment")
    with pytest.raises(PermissionViolation, match=r"repair\.permissions\.experiments"):
        enforce_permissions(plan, RepairPermissions())


async def test_mock_agent_applies_only_what_it_is_given(tmp_path: Path) -> None:
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    agent = MockRepairAgent()
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])
    result = await agent.repair(artifact, plan_with(), workspace)
    assert result.status == "completed"
    assert result.action_ids == ["ACTION-001"]
    assert result.evidence_used == ["Table 1 header reads 'latency'"]
    workspace.cleanup()


# ------------------------------------------------------------- generic CLI


async def test_generic_cli_agent_writes_files_and_invokes_the_command(tmp_path: Path) -> None:
    """A real subprocess edits the workspace; the adapter reports what changed."""
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, sys, pathlib\n"
        "prompt = pathlib.Path(sys.argv[1]).read_text()\n"
        "assert 'ACTION-001' in prompt\n"
        "assert 'Never invent' in prompt\n"
        "root = pathlib.Path(sys.argv[1]).parent.parent.parent\n"
        "(root / 'doc.md').write_text('repaired\\n')\n"
        "result = pathlib.Path(sys.argv[2])\n"
        "result.write_text(json.dumps({\n"
        "  'action_ids': ['ACTION-001'],\n"
        "  'status': 'completed',\n"
        "  'changes': [{'file': 'doc.md', 'description': 'stated the unit'}],\n"
        "  'evidence_used': ['Table 1 header']\n"
        "}))\n"
    )
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    config = RepairAgentConfig(
        provider="generic-cli",
        command=[sys.executable, str(script), "{prompt_file}", "{result_file}"],
        timeout=60,
    )
    agent = GenericCLIRepairAgent(config, RepairPermissions())
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])

    result = await agent.repair(artifact, plan_with(), workspace)

    assert result.status == "completed"
    assert (workspace.root / "doc.md").read_text() == "repaired\n"
    assert result.changed_files == ["doc.md"]
    assert result.changes[0].description == "stated the unit"
    workspace.cleanup()


async def test_generic_cli_agent_trusts_the_filesystem_over_the_agents_claim(
    tmp_path: Path,
) -> None:
    """An agent claiming a change it did not make does not get to record it."""
    script = tmp_path / "liar.py"
    script.write_text(
        "import json, sys, pathlib\n"
        "pathlib.Path(sys.argv[2]).write_text(json.dumps({\n"
        "  'action_ids': ['ACTION-001'], 'status': 'completed',\n"
        "  'changes': [{'file': 'doc.md', 'description': 'I fixed everything'}]\n"
        "}))\n"
    )
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    agent = GenericCLIRepairAgent(
        RepairAgentConfig(
            command=[sys.executable, str(script), "{prompt_file}", "{result_file}"], timeout=60
        ),
        RepairPermissions(),
    )
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])
    result = await agent.repair(artifact, plan_with(), workspace)
    assert result.changed_files == [], "nothing was actually changed on disk"
    workspace.cleanup()


async def test_generic_cli_agent_reports_a_missing_command(tmp_path: Path) -> None:
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    agent = GenericCLIRepairAgent(RepairAgentConfig(command=[]), RepairPermissions())
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])
    result = await agent.repair(artifact, plan_with(), workspace)
    assert result.status == "failed"
    assert "not configured" in result.notes[0]
    workspace.cleanup()


async def test_generic_cli_agent_handles_a_failing_command(tmp_path: Path) -> None:
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    agent = GenericCLIRepairAgent(
        RepairAgentConfig(command=[sys.executable, "-c", "import sys; sys.exit(9)"], timeout=60),
        RepairPermissions(),
    )
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])
    result = await agent.repair(artifact, plan_with(), workspace)
    assert result.status == "failed"
    assert result.metadata["exit_code"] == 9
    workspace.cleanup()


def test_the_repair_prompt_forbids_inventing_evidence() -> None:
    from veritas.repair import load_repair_prompt

    prompt = load_repair_prompt()
    assert "Never invent experimental evidence" in prompt
    assert "human_required" in prompt
    assert "You are not the judge" in prompt


def test_the_rendered_prompt_carries_permissions_and_no_judge_output(tmp_path: Path) -> None:
    agent = GenericCLIRepairAgent(RepairAgentConfig(), RepairPermissions())
    rendered = agent.render_prompt(plan_with(), tmp_path / "plan.json", tmp_path / "result.json")
    assert "Run experiments: NO" in rendered
    assert "ACTION-001" in rendered
    assert "doc.md" in rendered
    # The judge's own severity vocabulary and reasoning do not appear.
    assert "judge" not in rendered.lower().replace("you are not the judge", "")


def _artifact_dir(tmp_path: Path) -> Path:
    source = tmp_path / "artifact"
    if not source.exists():
        source.mkdir()
        (source / "doc.md").write_text("original\n")
    return source


def test_a_workspace_under_a_skipped_directory_still_sees_its_files(tmp_path: Path) -> None:
    """Regression: the skip list is relative to the root, not absolute.

    A repair workspace lives under `.veritas/workspaces/`, which is itself a
    skipped name. Matching absolute path parts hid every file in the workspace,
    so judges saw an empty artifact and checks reported the target unreadable.
    """
    source = tmp_path / "artifact"
    (source / "paper").mkdir(parents=True)
    (source / "paper" / "main.md").write_text("# Abstract\n\ntext\n")

    workspace = open_workspace(
        source, "copy", loop_id="l1", base_dir=source / ".veritas" / "workspaces"
    )
    assert ".veritas" in str(workspace.root)

    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["paper"])
    assert [segment.path for segment in artifact.segments()] == ["paper/main.md"]

    # Change detection has to survive the same trap.
    before = workspace.snapshot()
    assert before, "the workspace snapshot must not be empty"
    (workspace.root / "paper" / "main.md").write_text("# Abstract\n\nchanged\n")
    assert workspace.changed_since(before) == ["paper/main.md"]


def test_the_artifacts_own_veritas_directory_is_still_skipped(tmp_path: Path) -> None:
    source = tmp_path / "artifact"
    source.mkdir()
    (source / "doc.md").write_text("x\n")
    (source / ".veritas").mkdir()
    (source / ".veritas" / "leak.md").write_text("internal\n")

    artifact = Artifact(id="a", type="document", root=source, paths=["."])
    assert [segment.path for segment in artifact.segments()] == ["doc.md"]


@pytest.mark.skipif(not git_available(), reason="git is not installed")
def test_a_worktree_for_a_subdirectory_artifact_points_at_that_subdirectory(
    tmp_path: Path,
) -> None:
    """Regression: a worktree checks out the whole repository.

    With the artifact in `paper/`, the workspace root landed on the repository
    root, so every configured path resolved to nothing and the judges would
    have evaluated an empty artifact.
    """
    repo = tmp_path / "repo"
    (repo / "paper" / "evidence").mkdir(parents=True)
    (repo / "paper" / "manuscript.tex").write_text("\\documentclass{article}\n")
    (repo / "paper" / "evidence" / "data.txt").write_text("measured\n")
    (repo / "README.md").write_text("root\n")
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-qm", "initial"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    paper = repo / "paper"
    workspace = open_workspace(paper, "worktree", loop_id="l1", base_dir=tmp_path / "ws")
    if workspace.mode != "worktree":  # pragma: no cover - git too old
        pytest.skip("git worktree unavailable")

    assert (workspace.root / "manuscript.tex").is_file()
    artifact = Artifact(
        id="paper", type="document", root=workspace.root, paths=["manuscript.tex", "evidence"]
    )
    assert sorted(artifact.file_list()) == ["evidence/data.txt", "manuscript.tex"]

    # Changes are still tracked from the repository root, so work the agent
    # does outside the artifact is recorded rather than lost.
    before = workspace.snapshot()
    (workspace.root / "manuscript.tex").write_text("edited\n")
    assert workspace.changed_since(before) == ["paper/manuscript.tex"]
    assert "paper/manuscript.tex" in workspace.diff()
    assert (paper / "manuscript.tex").read_text() == "\\documentclass{article}\n"
    workspace.cleanup()


# ------------------------------------------- a no-op repair is a failure


async def test_an_agent_that_changes_nothing_is_a_failure(tmp_path: Path) -> None:
    """A misconfigured command exits zero and does nothing.

    That used to be recorded as `partial`, the loop carried straight past it,
    and the run claimed to have repaired actions never carried out.
    """
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    agent = GenericCLIRepairAgent(
        RepairAgentConfig(command=[sys.executable, "-c", "pass"], timeout=60),
        RepairPermissions(),
    )
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])

    result = await agent.repair(artifact, plan_with(), workspace)

    assert result.status == "failed"
    assert result.changes == []
    assert any("changed no file" in note for note in result.notes)
    # The command is named, so the operator can see what to fix.
    assert any(sys.executable in note for note in result.notes)
    workspace.cleanup()


async def test_an_agent_claiming_success_without_changing_a_file_is_a_failure(
    tmp_path: Path,
) -> None:
    """The changed-file list comes from disk, never from the agent's claim."""
    script = tmp_path / "liar.py"
    script.write_text(
        "import json, sys\n"
        "open(sys.argv[2], 'w').write(json.dumps({\n"
        "  'action_ids': ['A-1'], 'status': 'completed',\n"
        "  'changes': [{'file': 'doc.md', 'description': 'I definitely did this'}]\n"
        "}))\n"
    )
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    agent = GenericCLIRepairAgent(
        RepairAgentConfig(
            command=[sys.executable, str(script), "{prompt_file}", "{result_file}"], timeout=60
        ),
        RepairPermissions(),
    )
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])

    result = await agent.repair(artifact, plan_with(), workspace)

    assert result.status == "failed"
    assert any("changed no file" in note for note in result.notes)
    workspace.cleanup()


# ------------------------------------------- live agent output


async def test_agent_output_is_streamed_line_by_line(tmp_path: Path) -> None:
    """A repair runs for minutes; a spinner alone cannot be told from a hang."""
    seen: list[str] = []
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    agent = GenericCLIRepairAgent(
        RepairAgentConfig(
            command=[
                sys.executable,
                "-c",
                "print('reading the manuscript'); print('editing doc.md'); "
                "open('doc.md','a').write('x')",
            ],
            timeout=60,
        ),
        RepairPermissions(),
        on_output=seen.append,
    )
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])

    result = await agent.repair(artifact, plan_with(), workspace)

    assert seen == ["reading the manuscript", "editing doc.md"]
    # What was streamed is what is retained.
    assert "editing doc.md" in result.metadata["output"]
    workspace.cleanup()


async def test_a_silent_agent_streams_nothing(tmp_path: Path) -> None:
    seen: list[str] = []
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    agent = GenericCLIRepairAgent(
        RepairAgentConfig(
            command=[sys.executable, "-c", "open('doc.md','a').write('x')"], timeout=60
        ),
        RepairPermissions(),
        on_output=seen.append,
    )
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])
    result = await agent.repair(artifact, plan_with(), workspace)
    assert seen == []
    assert result.status == "completed"
    workspace.cleanup()


async def test_non_utf8_output_does_not_break_the_repair(tmp_path: Path) -> None:
    """An agent may emit progress bytes that are not valid UTF-8 mid-line."""
    seen: list[str] = []
    workspace = temporary_workspace(_artifact_dir(tmp_path))
    agent = GenericCLIRepairAgent(
        RepairAgentConfig(
            command=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'caf\\xe9\\n'); sys.stdout.flush(); "
                "open('doc.md','a').write('x')",
            ],
            timeout=60,
        ),
        RepairPermissions(),
        on_output=seen.append,
    )
    artifact = Artifact(id="a", type="document", root=workspace.root, paths=["doc.md"])
    result = await agent.repair(artifact, plan_with(), workspace)
    assert seen and seen[0].startswith("caf")
    assert result.status == "completed"
    workspace.cleanup()
