"""CLI behaviour and, above all, exit codes — CI depends on them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from veritas.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")


def write_project(tmp_path: Path, base_url: str, repo_root: Path, **gate: Any) -> Path:
    (tmp_path / "doc.md").write_text("# Design\n\nThe system is fast and secure.\n")
    gate_yaml = "\n".join(f"  {key}: {value}" for key, value in gate.items())
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
{gate_yaml}
"""
    )
    return tmp_path


def responder(payload: dict[str, Any]):
    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, {"choices": [{"message": {"content": json.dumps(payload)}}]}

    return handler


CLEAN = {"status": "pass", "summary": "No issues found.", "findings": []}
MINOR = {
    "status": "warning",
    "findings": [
        {
            "title": "Terminology drifts between sections",
            "severity": "minor",
            "category": "clarity",
            "description": "d",
            "evidence": ["e"],
        }
    ],
}
CRITICAL = {
    "status": "fail",
    "findings": [
        {
            "title": "Reported result cannot be produced by the artifact",
            "severity": "critical",
            "category": "integrity",
            "description": "d",
            "evidence": ["e"],
        }
    ],
}


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "veritas-gate" in result.stdout


def test_exit_code_0_on_pass(tmp_path: Path, serve, repo_root) -> None:
    with serve(responder(CLEAN)) as server:
        project = write_project(tmp_path, server.base_url, repo_root, max_major=0)
        result = runner.invoke(app, ["evaluate", str(project)])
    assert result.exit_code == 0, result.stdout
    assert "PASS" in result.stdout


def test_exit_code_1_on_warnings(tmp_path: Path, serve, repo_root) -> None:
    with serve(responder(MINOR)) as server:
        project = write_project(tmp_path, server.base_url, repo_root, max_major=0)
        result = runner.invoke(app, ["evaluate", str(project)])
    assert result.exit_code == 1
    assert "PASS_WITH_WARNINGS" in result.stdout


def test_exit_code_2_on_revise(tmp_path: Path, serve, repo_root, judge_payload) -> None:
    with serve(responder(judge_payload)) as server:
        project = write_project(tmp_path, server.base_url, repo_root, max_major=0)
        result = runner.invoke(app, ["evaluate", str(project)])
    assert result.exit_code == 2
    assert "REVISE" in result.stdout


def test_exit_code_3_on_fail(tmp_path: Path, serve, repo_root) -> None:
    with serve(responder(CRITICAL)) as server:
        project = write_project(tmp_path, server.base_url, repo_root, max_major=0)
        result = runner.invoke(app, ["evaluate", str(project)])
    assert result.exit_code == 3
    assert "FAIL" in result.stdout


def test_exit_code_4_on_configuration_error(tmp_path: Path, repo_root) -> None:
    (tmp_path / "doc.md").write_text("x")
    (tmp_path / "veritas.yaml").write_text("profile: no-such-profile\n")
    result = runner.invoke(app, ["evaluate", str(tmp_path)])
    assert result.exit_code == 4
    assert "unknown profile" in result.stderr


def test_missing_artifact_path_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["evaluate", str(tmp_path / "absent")])
    assert result.exit_code == 4


def test_reporting_commands_read_the_persisted_run(
    tmp_path: Path, serve, repo_root, judge_payload
) -> None:
    with serve(responder(judge_payload)) as server:
        project = write_project(tmp_path, server.base_url, repo_root, max_major=0)
        assert runner.invoke(app, ["evaluate", str(project)]).exit_code == 2

    report = runner.invoke(app, ["report", str(project), "--raw"])
    assert report.exit_code == 0
    assert "Veritas Gate Report" in report.stdout

    findings = runner.invoke(app, ["findings", str(project), "--json"])
    assert findings.exit_code == 0
    payload = json.loads(findings.stdout)
    assert any(item["severity"] == "major" for item in payload)

    filtered = runner.invoke(app, ["findings", str(project), "--severity", "major", "--json"])
    assert all(item["severity"] in ("major", "critical") for item in json.loads(filtered.stdout))

    claims = runner.invoke(app, ["claims", str(project), "--json"])
    assert json.loads(claims.stdout)["coverage"]["total"] == 2

    gate = runner.invoke(app, ["gate", str(project)])
    assert gate.exit_code == 2
    assert gate.stdout.strip() == "REVISE"

    # The run directory keeps its own advisory plan...
    run_dir = (
        project
        / ".veritas"
        / "runs"
        / (project / ".veritas" / "runs" / "latest").read_text().strip()
    )
    assert json.loads((run_dir / "repair-plan.json").read_text())["actions"]

    # ...and assist mode plans from that stored run without re-evaluating.
    plan = runner.invoke(app, ["repair-plan", str(project), "--no-evaluate", "--json"])
    assert plan.exit_code == 0, plan.stdout
    assert json.loads(plan.stdout)["actions"]


def test_reporting_without_a_run_is_an_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["report", str(tmp_path)])
    assert result.exit_code == 4
    assert "no evaluation runs found" in result.stderr


def test_invalid_severity_filter_is_rejected(tmp_path: Path, serve, repo_root) -> None:
    with serve(responder(CLEAN)) as server:
        project = write_project(tmp_path, server.base_url, repo_root, max_major=0)
        runner.invoke(app, ["evaluate", str(project)])
    result = runner.invoke(app, ["findings", str(project), "--severity", "huge"])
    assert result.exit_code == 4


def test_init_writes_a_starter_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo_root
) -> None:
    monkeypatch.setenv("VERITAS_PROFILE_PATH", str(repo_root / "profiles"))
    result = runner.invoke(app, ["init", str(tmp_path), "--profile", "generic-document"])
    assert result.exit_code == 0
    config = (tmp_path / "veritas.yaml").read_text()
    assert "profile: generic-document" in config
    assert "allow: []" in config, "execution must be empty by default"
    assert (tmp_path / ".veritas" / "runs").is_dir()

    again = runner.invoke(app, ["init", str(tmp_path), "--profile", "generic-document"])
    assert again.exit_code == 4
    assert "already exists" in again.stderr


def test_init_rejects_an_unknown_profile(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--profile", "nope"])
    assert result.exit_code == 4


def test_profiles_command_lists_the_bundled_profiles(repo_root: Path) -> None:
    result = runner.invoke(app, ["profiles", str(repo_root)])
    assert result.exit_code == 0
    assert "scientific-paper" in result.stdout
    assert "generic-document" in result.stdout


def test_an_unreachable_provider_never_reports_a_pass(tmp_path: Path, repo_root) -> None:
    """Regression, end to end: bad credentials produced `Gate: PASS`, exit 0.

    Every judge errors, so there are no artifact findings. The gate must treat
    that as an evaluation that did not happen, not as an artifact with no faults.
    """
    project = write_project(tmp_path, "http://127.0.0.1:1", repo_root, max_major=0)
    result = runner.invoke(app, ["evaluate", str(project)])

    assert result.exit_code == 3, result.stdout
    assert "FAIL" in result.stdout
    assert "could not complete" in result.stdout
    assert "not fully evaluated" in result.stdout

    runs = project / ".veritas" / "runs"
    run_dir = runs / (runs / "latest").read_text().strip()
    gate = json.loads((run_dir / "gate.json").read_text())
    assert gate["status"] == "FAIL"
    assert sorted(gate["judge_errors"]) == ["adversarial", "evidence", "structure"]
