"""The benchmark: known issues in, recall and unexplained findings out."""

from __future__ import annotations

import gzip
import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from veritas.benchmark import Case, KnownIssue, Source, load_manifest, unpack_arxiv
from veritas.cli import app
from veritas.config import ConfigError
from veritas.models.finding import Finding

runner = CliRunner()


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")


def finding(title: str, severity: str = "major", **kwargs: Any) -> Finding:
    return Finding(
        id=kwargs.pop("id", "F-1"),
        title=title,
        severity=severity,
        category="c",
        description=kwargs.pop("description", "d"),
        **kwargs,
    )  # type: ignore[arg-type]


def test_a_url_is_read_as_its_kind_of_source() -> None:
    assert Source.from_url("https://arxiv.org/abs/2401.12345v2").arxiv == "2401.12345v2"
    assert Source.from_url("https://arxiv.org/pdf/2401.12345.pdf").arxiv == "2401.12345"
    assert Source.from_url("arxiv:2401.12345").arxiv == "2401.12345"
    assert Source.from_url("https://github.com/o/r").git == "https://github.com/o/r"
    assert Source.from_url("planted/x").path == "planted/x"
    case = Case.model_validate({"id": "a", "source": "https://arxiv.org/abs/1"})
    assert case.source.arxiv == "1"


def test_every_pattern_must_match_and_severity_counts() -> None:
    issue = KnownIssue(id="i", description="d", match=["87", "abstract"], min_severity="major")
    assert issue.matches(finding("Abstract says 87%"))
    assert issue.matches(finding("Mismatch", evidence=["abstract: 87%"]))
    assert not issue.matches(finding("Abstract overclaims"))
    # Caught as a nit is not caught.
    assert not issue.matches(finding("Abstract says 87%", severity="minor"))


def test_the_starter_manifest_loads(repo_root: Path) -> None:
    manifest = load_manifest(repo_root / "benchmarks" / "manifest.yaml")
    assert manifest.cases[0].id == "planted-overclaim"
    assert len(manifest.cases[0].known_issues) == 4


def test_a_bad_manifest_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / "m.yaml").write_text("cases: [{id: x, source: {path: a, git: b}}]\n")
    with pytest.raises(ConfigError, match="exactly one"):
        load_manifest(tmp_path / "m.yaml")


def test_arxiv_sources_unpack_safely(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        data = b"\\documentclass{article}"
        info = tarfile.TarInfo("main.tex")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        evil = tarfile.TarInfo("../escape.tex")
        evil.size = 1
        archive.addfile(evil, io.BytesIO(b"x"))
    with pytest.raises(tarfile.TarError):
        unpack_arxiv(buffer.getvalue(), tmp_path / "t")
    assert not (tmp_path / "escape.tex").exists()

    unpack_arxiv(gzip.compress(b"\\documentclass{article}\n"), tmp_path / "single")
    assert (tmp_path / "single" / "main.tex").is_file()

    with pytest.raises(ConfigError, match="only a PDF"):
        unpack_arxiv(b"%PDF-1.5 ...", tmp_path / "pdf")


def test_benchmark_runs_scores_and_rescores(tmp_path: Path, serve, repo_root: Path) -> None:
    shutil.copytree(repo_root / "benchmarks", tmp_path / "benchmarks")
    payload = {
        "status": "fail",
        "findings": [
            {
                "title": "Abstract accuracy (87%) contradicts the table (12/16)",
                "severity": "major",
                "category": "evidence",
                "description": "12/16 is 75%.",
                "evidence": ["Abstract: 87%", "Results: 12/16"],
            },
            {
                "title": "Title is long",
                "severity": "minor",
                "category": "clarity",
                "description": "d",
                "evidence": ["title"],
            },
        ],
    }

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, {"choices": [{"message": {"content": json.dumps(payload)}}]}

    with serve(handler) as server:
        (tmp_path / "veritas.yaml").write_text(
            f"profile_paths: [{repo_root / 'profiles'}]\n"
            "models:\n  default:\n    provider: openai\n    model: m\n"
            f"    base_url: {server.base_url}\n    max_retries: 1\n"
            "judge_paths: {evidence: [nothing]}\n"
        )
        outcome = runner.invoke(
            app,
            [
                "benchmark",
                str(tmp_path / "benchmarks" / "manifest.yaml"),
                "--config",
                str(tmp_path / "veritas.yaml"),
            ],
        )
    assert outcome.exit_code == 0, outcome.output
    out = next((tmp_path / ".veritas" / "benchmark").glob("2*"))
    score = json.loads((out / "score.json").read_text())
    issues = {i["id"]: i["detected"] for i in score["cases"][0]["issues"]}
    assert issues["accuracy-mismatch"] is True  # from the judges
    assert issues["dangling-results"] is True  # from reference-integrity, no model
    assert issues["generalisation"] is False
    assert any("Title is long" in item for item in score["cases"][0]["unmatched"])
    assert "Detected" in outcome.output

    # Refining a pattern costs nothing: rescore the saved runs.
    manifest = tmp_path / "benchmarks" / "manifest.yaml"
    manifest.write_text(manifest.read_text().replace('"results\\\\.csv"', '"nomatch"'))
    again = runner.invoke(app, ["benchmark", str(manifest), "--rescore", str(out)])
    assert again.exit_code == 0, again.output
    rescored = json.loads((out / "score.json").read_text())
    assert {i["id"]: i["detected"] for i in rescored["cases"][0]["issues"]}[
        "dangling-results"
    ] is False
