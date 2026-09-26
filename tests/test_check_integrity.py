"""Frozen evidence is append-only; release metadata agrees with itself."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from veritas.artifacts.base import Artifact
from veritas.checks import build_check
from veritas.config import CheckConfig, ExecutionConfig

pytestmark = pytest.mark.asyncio


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "report.md").write_text("350 rounds\n", encoding="utf-8")
    (tmp_path / "evidence" / "README.md").write_text("index\n", encoding="utf-8")
    (tmp_path / "CITATION.cff").write_text(
        "version: 0.4.0\ndoi: 10.5281/zenodo.1\nidentifiers:\n- value: 10.5281/zenodo.2\n",
        encoding="utf-8",
    )
    (tmp_path / "paper.md").write_text("Archived at 10.5281/zenodo.2.\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "freeze")
    git(tmp_path, "tag", "v0.4.0")
    return tmp_path


def run(root: Path, frozen: list[str]):
    config = CheckConfig(type="frozen-integrity", target="paper.md")
    check = build_check("integrity", config, ExecutionConfig(), [], frozen)
    return check.run(Artifact(id="a", type="paper", root=root, paths=["."]))


def titles(result) -> list[str]:
    return [finding.title for finding in result.findings]


async def test_untouched_frozen_evidence_and_consistent_metadata_pass(tmp_path: Path) -> None:
    result = await run(repo(tmp_path), ["evidence/**"])
    assert result.status == "pass", titles(result)


async def test_a_commit_that_edits_frozen_evidence_is_reported(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "evidence" / "report.md").write_text("315 rounds\n", encoding="utf-8")
    git(root, "commit", "-qam", "fixing")
    result = await run(root, ["evidence/**"])
    assert titles(result) == ["Frozen evidence was modified after it was committed"]
    assert "fixing" in result.findings[0].evidence[0]


async def test_adding_a_correction_file_is_allowed(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "evidence" / "corrections").mkdir()
    (root / "evidence" / "corrections" / "v1.md").write_text("erratum\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "append correction")
    assert (await run(root, ["evidence/**"])).status == "pass"


async def test_an_uncommitted_edit_is_reported(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "evidence" / "report.md").write_text("edited\n", encoding="utf-8")
    assert titles(await run(root, ["evidence/**"])) == [
        "1 frozen file(s) modified in the working tree"
    ]


async def test_exclusions_leave_mutable_files_alone(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "evidence" / "README.md").write_text("new index\n", encoding="utf-8")
    git(root, "commit", "-qam", "update index")
    assert (await run(root, ["evidence/**", "!evidence/README.md"])).status == "pass"


async def test_release_metadata_disagreements_are_reported(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "CITATION.cff").write_text("version: 0.5.0\ndoi: 10.5281/zenodo.1\n", encoding="utf-8")
    result = await run(root, [])
    assert titles(result) == [
        "CITATION.cff version 0.5.0 has no matching git tag",
        "The manuscript cites an archive DOI that CITATION.cff does not list",
    ]


async def test_outside_git_it_is_skipped(tmp_path: Path) -> None:
    assert (await run(tmp_path, ["evidence/**"])).status == "skipped"
