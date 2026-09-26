"""End-to-end pipeline over a real artifact and a local provider endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from veritas.artifacts import Artifact
from veritas.config import ConfigError, load_config
from veritas.engine import Engine, EngineOptions
from veritas.profiles import load_profile
from veritas.reports import render_markdown
from veritas.runs import latest_run_dir, load_run, unique_run_dir, write_run


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")


def responder(payload: dict[str, Any]):
    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, {"choices": [{"message": {"content": json.dumps(payload)}}]}

    return handler


def config_for(tmp_path: Path, base_url: str, repo_root: Path, **overrides: Any):
    text = f"""
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
"""
    path = tmp_path / "veritas.yaml"
    path.write_text(text)
    config = load_config(path)
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def artifact_for(tmp_path: Path) -> Artifact:
    (tmp_path / "doc.md").write_text("# Design\n\nThe system is fast and secure.\n")
    return Artifact(id="doc", type="document", root=tmp_path, paths=["doc.md"])


async def test_full_pipeline_produces_a_run(
    tmp_path: Path, serve, judge_payload, repo_root
) -> None:
    with serve(responder(judge_payload)) as server:
        config = config_for(tmp_path, server.base_url, repo_root)
        profile = load_profile("generic-document", repo_root)
        result = await Engine(config, profile).run(artifact_for(tmp_path))

    # Three judges ran independently.
    assert {item.judge for item in result.judge_results} == {
        "structure",
        "evidence",
        "adversarial",
    }
    # The same finding reported by all three collapses into one consolidated item.
    titles = [item.finding.title for item in result.meta_review.consolidated]
    assert titles.count("Headline improvement is unsupported") == 1
    consolidated = next(
        item
        for item in result.meta_review.consolidated
        if item.finding.title == "Headline improvement is unsupported"
    )
    assert len(consolidated.reported_by) == 3
    assert consolidated.consensus == "confirmed"
    assert consolidated.consensus_confidence == "high"
    # Claims came only from the claim-extracting judge.
    assert result.coverage.total == 2
    assert result.coverage.unsupported == 1
    # A major finding with max_major 0 means REVISE, not FAIL.
    assert result.gate.status == "REVISE"
    assert result.gate.exit_code == 2
    # Reproducibility metadata is recorded.
    assert result.manifest.profile == "generic-document"
    assert result.manifest.prompt_versions
    # Three judges plus the MetaJudge's own call, which used to go uncounted.
    assert result.manifest.usage["calls"] == 4
    assert {entry["model"] for entry in result.manifest.usage["by_model"]}
    assert result.manifest.usage["cost"] is None  # no pricing configured


async def test_a_critical_finding_from_one_judge_fails_the_gate(
    tmp_path: Path, serve, repo_root
) -> None:
    """Judge A critical, judges B and C silent: the gate still fails."""
    calls: list[int] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        calls.append(1)
        if len(calls) == 1:
            payload = {
                "status": "fail",
                "findings": [
                    {
                        "title": "Experimental result is fabricated",
                        "severity": "critical",
                        "category": "integrity",
                        "description": "The reported result cannot be produced by the artifact.",
                        "evidence": ["no script produces this number"],
                        "confidence": 0.95,
                    }
                ],
            }
        else:
            payload = {"status": "pass", "summary": "Looks fine to me.", "findings": []}
        return 200, {"choices": [{"message": {"content": json.dumps(payload)}}]}

    with serve(handler) as server:
        config = config_for(tmp_path, server.base_url, repo_root)
        config.concurrency = 1  # deterministic ordering for this assertion
        profile = load_profile("generic-document", repo_root)
        result = await Engine(config, profile).run(artifact_for(tmp_path))

    assert result.gate.status == "FAIL"
    assert result.gate.critical == 1
    assert result.meta_review.disagreements, "a lone critical must be recorded as a disagreement"


async def test_judge_filter_and_repeated_runs_record_stability(
    tmp_path: Path, serve, repo_root
) -> None:
    calls: list[int] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        calls.append(1)
        severity = "major" if len(calls) < 3 else "info"
        payload = {
            "findings": [
                {
                    "title": "Claim lacks support",
                    "severity": severity,
                    "category": "evidence",
                    "description": "d",
                    "evidence": ["e"],
                }
            ]
        }
        return 200, {"choices": [{"message": {"content": json.dumps(payload)}}]}

    with serve(handler) as server:
        config = config_for(tmp_path, server.base_url, repo_root)
        config.concurrency = 1
        profile = load_profile("generic-document", repo_root)
        options = EngineOptions(judge_filter=["evidence"], runs=3)
        result = await Engine(config, profile, options).run(artifact_for(tmp_path))

    assert len(result.judge_results) == 1
    assert result.stability[0].runs == 3
    assert result.stability[0].stability == "MEDIUM"
    # The harshest verdict is kept; repeated runs never average a concern away.
    assert result.judge_results[0].findings[0].severity == "major"


async def test_unknown_judge_is_rejected(tmp_path: Path, serve, judge_payload, repo_root) -> None:
    from veritas.config import ConfigError

    with serve(responder(judge_payload)) as server:
        config = config_for(tmp_path, server.base_url, repo_root)
        profile = load_profile("generic-document", repo_root)
        engine = Engine(config, profile, EngineOptions(judge_filter=["nonexistent"]))
        with pytest.raises(ConfigError, match="unknown judge"):
            await engine.run(artifact_for(tmp_path))


async def test_run_is_written_and_read_back(
    tmp_path: Path, serve, judge_payload, repo_root
) -> None:
    with serve(responder(judge_payload)) as server:
        config = config_for(tmp_path, server.base_url, repo_root)
        profile = load_profile("generic-document", repo_root)
        result = await Engine(config, profile).run(artifact_for(tmp_path))

    run_dir = unique_run_dir(config.runs_dir(), result.manifest.run_id)
    write_run(run_dir, result, render_markdown(result))

    for name in (
        "manifest.json",
        "results.json",
        "gate.json",
        "claims.json",
        "report.md",
        "repair-plan.json",
    ):
        assert (run_dir / name).is_file(), name
    assert (run_dir / "judges" / "evidence.json").is_file()

    assert latest_run_dir(config.runs_dir()) == run_dir
    reloaded = load_run(run_dir)
    assert reloaded.gate.status == result.gate.status
    assert len(reloaded.meta_review.consolidated) == len(result.meta_review.consolidated)

    plan = json.loads((run_dir / "repair-plan.json").read_text())
    assert plan["actions"], "a REVISE run must propose actions"
    assert "does not modify artifacts" in plan["note"]


async def test_runs_are_immutable(tmp_path: Path, serve, judge_payload, repo_root) -> None:
    """A second run never overwrites the first."""
    with serve(responder(judge_payload)) as server:
        config = config_for(tmp_path, server.base_url, repo_root)
        profile = load_profile("generic-document", repo_root)
        result = await Engine(config, profile).run(artifact_for(tmp_path))

    first = unique_run_dir(config.runs_dir(), result.manifest.run_id)
    second = unique_run_dir(config.runs_dir(), result.manifest.run_id)
    assert first != second
    assert first.is_dir() and second.is_dir()


async def test_markdown_report_contains_every_required_section(
    tmp_path: Path, serve, judge_payload, repo_root
) -> None:
    with serve(responder(judge_payload)) as server:
        config = config_for(tmp_path, server.base_url, repo_root)
        profile = load_profile("generic-document", repo_root)
        result = await Engine(config, profile).run(artifact_for(tmp_path))

    markdown = render_markdown(result)
    for heading in (
        "## Executive summary",
        "## Gate result",
        "## Blocking findings",
        "## Judge consensus",
        "## Judge disagreements",
        "## Claim coverage",
        "## Deterministic checks",
        "## All findings",
        "## Recommended changes",
        "## Evaluation metadata",
    ):
        assert heading in markdown, heading


async def test_missing_model_config_is_a_clear_error(tmp_path: Path, repo_root) -> None:
    from veritas.config import ConfigError

    (tmp_path / "veritas.yaml").write_text(
        f"profile: generic-document\nprofile_paths:\n  - {repo_root / 'profiles'}\n"
    )
    config = load_config(tmp_path / "veritas.yaml")
    profile = load_profile("generic-document", repo_root)
    with pytest.raises(ConfigError, match="no model configured"):
        await Engine(config, profile).run(artifact_for(tmp_path))


# ------------------------------------------- per-judge scope


def _two_file_artifact(tmp_path: Path) -> Artifact:
    (tmp_path / "doc.md").write_text("# Design\n", encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "code.ts").write_text("export const x = 1;\n", encoding="utf-8")
    return Artifact(id="doc", type="document", root=tmp_path, paths=["doc.md", "src"])


def test_a_judge_named_in_judge_paths_sees_only_those_segments(
    tmp_path: Path, repo_root: Path
) -> None:
    """Every judge reading everything cost ~300k input tokens per judge."""
    config = config_for(
        tmp_path, "http://127.0.0.1:9", repo_root, judge_paths={"structure": ["doc.md"]}
    )
    engine = Engine(config, load_profile("generic-document", repo_root))
    artifact = _two_file_artifact(tmp_path)

    scoped = engine._scoped_artifacts(artifact, engine.build_judges())

    assert [s.path for s in scoped["structure"].segments()] == ["doc.md"]
    # A judge not named keeps the whole artifact.
    assert "evidence" not in scoped
    assert {s.path for s in artifact.segments()} == {"doc.md", "src/code.ts"}


def test_judge_paths_cannot_widen_beyond_the_artifact(tmp_path: Path, repo_root: Path) -> None:
    (tmp_path / "secret.txt").write_text("not offered\n", encoding="utf-8")
    config = config_for(
        tmp_path, "http://127.0.0.1:9", repo_root, judge_paths={"structure": ["secret.txt"]}
    )
    engine = Engine(config, load_profile("generic-document", repo_root))
    scoped = engine._scoped_artifacts(_two_file_artifact(tmp_path), engine.build_judges())
    assert scoped["structure"].segments() == []


def test_an_unknown_judge_in_judge_paths_is_rejected(tmp_path: Path, repo_root: Path) -> None:
    config = config_for(
        tmp_path, "http://127.0.0.1:9", repo_root, judge_paths={"nonexistent": ["doc.md"]}
    )
    engine = Engine(config, load_profile("generic-document", repo_root))
    with pytest.raises(ConfigError, match="nonexistent"):
        engine._scoped_artifacts(_two_file_artifact(tmp_path), engine.build_judges())


def test_a_scoped_latex_file_brings_its_inputs(tmp_path: Path, repo_root: Path) -> None:
    (tmp_path / "main.tex").write_text("\\input{table}\n", encoding="utf-8")
    (tmp_path / "table.tex").write_text("rows\n", encoding="utf-8")
    (tmp_path / "doc.md").write_text("# d\n", encoding="utf-8")
    config = config_for(
        tmp_path, "http://127.0.0.1:9", repo_root, judge_paths={"structure": ["main.tex"]}
    )
    engine = Engine(config, load_profile("generic-document", repo_root))
    artifact = Artifact(id="a", type="document", root=tmp_path, paths=["main.tex", "doc.md"])
    scoped = engine._scoped_artifacts(artifact, engine.build_judges())
    assert {s.path for s in scoped["structure"].segments()} == {"main.tex", "table.tex"}
