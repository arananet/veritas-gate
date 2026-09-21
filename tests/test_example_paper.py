"""The flawed example paper is the project's end-to-end demo and regression test.

The provider endpoint here returns the findings a competent reviewer would
reach for this artifact; the point of the test is that the *pipeline* turns
those into a non-PASS gate with the right blocking findings and reports.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from veritas.cli import app

runner = CliRunner()

JUDGE_REPLIES: dict[str, dict[str, Any]] = {
    "methodology": {
        "status": "fail",
        "summary": "The experimental design cannot support the paper's conclusion.",
        "findings": [
            {
                "title": "No baseline tuning or workload justification is reported",
                "severity": "major",
                "category": "experimental-design",
                "description": (
                    "The comparison is against least-connections routing on a synthetic "
                    "workload, with no justification that the workload is representative."
                ),
                "location": "paper/main.md#experimental-setup",
                "evidence": [
                    "Experimental setup names only a synthetic generator and 8 backends.",
                    "No service-time distribution or tuning procedure is given.",
                ],
                "recommendation": "Justify the workload and report baseline tuning.",
                "confidence": 0.8,
            }
        ],
    },
    "evidence": {
        "status": "fail",
        "summary": "The headline number is contradicted by the artifact.",
        "findings": [
            {
                "title": "Headline latency reduction is contradicted by the results file",
                "severity": "critical",
                "category": "evidence",
                "description": (
                    "The paper reports 41ms p99 for FastRoute; results/benchmark.json "
                    "records 48.3ms, which is a 25.7% reduction, not 37%."
                ),
                "location": "paper/main.md#results",
                "evidence": [
                    "paper/main.md Results table: FastRoute 41ms",
                    "results/benchmark.json: fastroute.p99_ms = 48.3",
                ],
                "recommendation": "Reconcile the manuscript with the benchmark artifact.",
                "confidence": 0.95,
            },
            {
                "title": "Secondary claims have no supporting data",
                "severity": "major",
                "category": "evidence",
                "description": (
                    "Mean-latency improvement and robustness under bursty arrivals are "
                    "asserted with no corresponding experiment."
                ),
                "location": "paper/main.md#results",
                "evidence": ["No burst experiment appears in results/ or scripts/."],
                "confidence": 0.85,
            },
        ],
        "claims": [
            {
                "text": "FastRoute reduces p99 latency by 37%.",
                "source_location": "paper/main.md#abstract",
                "importance": "major",
                "status": "unsupported",
                "evidence": ["results/benchmark.json records 48.3ms, a 25.7% reduction"],
                "rationale": "The supporting artifact contradicts the reported figure.",
            },
            {
                "text": "FastRoute applies to any request-routing workload.",
                "source_location": "paper/main.md#abstract",
                "importance": "major",
                "status": "unsupported",
                "evidence": ["Only one synthetic workload with 8 backends was evaluated."],
                "rationale": "Generality is claimed from a single setting.",
            },
            {
                "text": "Throughput is unchanged.",
                "source_location": "paper/main.md#results",
                "importance": "minor",
                "status": "unverified",
                "evidence": [],
            },
        ],
    },
    "statistics": {
        "status": "fail",
        "findings": [
            {
                "title": "Improvement is based on a single run with no variance",
                "severity": "major",
                "category": "statistics",
                "description": (
                    "One run, no seed and no variance are reported, yet the difference "
                    "is presented as an improvement."
                ),
                "location": "results/benchmark.json",
                "evidence": [
                    'results/benchmark.json: "runs": 1',
                    'results/benchmark.json: "seed": null',
                    "scripts/benchmark.py calls random without seeding.",
                ],
                "recommendation": "Run multiple seeds and report mean and standard deviation.",
                "confidence": 0.95,
            }
        ],
    },
    "reproducibility": {
        "status": "fail",
        "findings": [
            {
                "title": "No environment, dependencies or hardware are specified",
                "severity": "major",
                "category": "reproducibility",
                "description": (
                    "The artifact gives no Python version, no dependency list and no "
                    "hardware description, so the latency numbers cannot be reproduced."
                ),
                "location": "paper/main.md#experimental-setup",
                "evidence": [
                    "No lockfile or requirements file is present.",
                    "Latency is claimed but no hardware is named.",
                ],
                "confidence": 0.9,
            }
        ],
    },
    "repo-consistency": {
        "status": "fail",
        "findings": [
            {
                "title": "Implementation does not match the described method",
                "severity": "critical",
                "category": "consistency",
                "description": (
                    "The paper describes a gradient-boosted queue-depth estimator; "
                    "experiments/router.py implements an EWMA."
                ),
                "location": "experiments/router.py",
                "evidence": [
                    "paper/main.md Method: 'gradient-boosted regression model'",
                    "experiments/router.py: class EwmaRouter",
                ],
                "recommendation": "Correct the manuscript or the implementation.",
                "confidence": 0.95,
            }
        ],
    },
    "citations": {
        "status": "warning",
        "findings": [
            {
                "title": "References cannot be verified from the artifact",
                "severity": "minor",
                "category": "citations",
                "description": (
                    "Neither reference carries a DOI or URL, and both venues are generic; "
                    "existence cannot be confirmed from the material supplied."
                ),
                "location": "paper/main.md#references",
                "evidence": [
                    "[Chen2021] has no DOI.",
                    "[Okafor2019] names a generic conference.",
                ],
                "confidence": 0.5,
            }
        ],
    },
    "adversarial": {
        "status": "fail",
        "findings": [
            {
                "title": "Generality claim is unsupported by a single-setting evaluation",
                "severity": "major",
                "category": "overclaiming",
                "description": (
                    "The abstract and conclusion claim applicability to any routing "
                    "workload on the basis of one synthetic 8-backend experiment."
                ),
                "location": "paper/main.md#conclusion",
                "evidence": [
                    "Abstract: 'applies to any request-routing workload'",
                    "Only one workload is evaluated.",
                ],
                "confidence": 0.9,
            },
            {
                "title": "Headline number disagrees with the supplied benchmark",
                "severity": "critical",
                "category": "integrity",
                "description": "The p99 figure in the paper is not the figure in the artifact.",
                "location": "paper/main.md#results",
                "evidence": ["paper: 41ms", "results/benchmark.json: 48.3ms"],
                "confidence": 0.9,
            },
        ],
    },
}


def judge_for_prompt(system: str) -> str:
    """Identify which judge is calling from its prompt heading."""
    for name, heading in (
        ("methodology", "# Methodology judge"),
        ("evidence", "# Evidence judge"),
        ("statistics", "# Statistics judge"),
        ("reproducibility", "# Reproducibility judge"),
        ("repo-consistency", "# Repository / manuscript consistency judge"),
        ("citations", "# Citation judge"),
        ("adversarial", "# Adversarial reviewer"),
    ):
        if heading in system:
            return name
    raise AssertionError("unrecognised judge prompt")


@pytest.fixture
def example_project(tmp_path: Path, repo_root: Path, serve) -> Any:
    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        system = body["messages"][0]["content"]
        payload = JUDGE_REPLIES[judge_for_prompt(system)]
        return 200, {"choices": [{"message": {"content": json.dumps(payload)}}]}

    project = tmp_path / "paper"
    shutil.copytree(repo_root / "examples" / "paper", project)
    with serve(handler) as server:
        (project / "veritas.yaml").write_text(
            (project / "veritas.yaml")
            .read_text()
            .replace("${VERITAS_DEFAULT_PROVIDER:-anthropic}", "openai")
            .replace("${VERITAS_DEFAULT_MODEL:-claude-sonnet-4-5}", "test-model")
            .replace("${VERITAS_ADVERSARIAL_PROVIDER:-openai}", "openai")
            .replace("${VERITAS_ADVERSARIAL_MODEL:-gpt-4.1}", "test-model")
            .replace("${VERITAS_META_PROVIDER:-google}", "openai")
            .replace("${VERITAS_META_MODEL:-gemini-2.5-pro}", "test-model")
            + f"\nprofile_paths:\n  - {repo_root / 'profiles'}\n"
        )
        # Route every role at the local endpoint.
        text = (
            (project / "veritas.yaml")
            .read_text()
            .replace(
                "    model: test-model",
                f"    model: test-model\n    base_url: {server.base_url}\n    max_retries: 1",
            )
        )
        (project / "veritas.yaml").write_text(text)
        yield project


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")


def test_the_flawed_paper_does_not_pass(example_project: Path) -> None:
    result = runner.invoke(app, ["evaluate", str(example_project)])
    assert result.exit_code == 3, result.stdout
    assert "FAIL" in result.stdout

    run = json.loads(
        (
            example_project / ".veritas" / "runs" / _latest(example_project) / "results.json"
        ).read_text()
    )
    gate = run["gate"]
    assert gate["status"] == "FAIL"
    assert gate["critical"] >= 1

    titles = {item["finding"]["title"] for item in run["meta_review"]["consolidated"]}
    assert "Implementation does not match the described method" in titles
    assert any("Headline latency reduction" in title for title in titles)

    # The two judges that independently caught the headline mismatch are merged,
    # and the merged finding keeps the critical severity.
    headline = next(
        item
        for item in run["meta_review"]["consolidated"]
        if "Headline" in item["finding"]["title"]
    )
    assert set(headline["reported_by"]) == {"evidence", "adversarial"}
    assert headline["finding"]["severity"] == "critical"
    assert headline["consensus"] == "confirmed"

    # The claim graph saw the unsupported claims.
    coverage = run["coverage"]
    assert coverage["total"] == 3
    assert coverage["unsupported"] == 2
    assert coverage["coverage"] == 0.0

    # The deterministic checks ran without executing anything.
    checks = {item["check"]: item["status"] for item in run["check_results"]}
    assert checks["repository-structure"] == "pass"
    assert checks["required-sections"] == "pass"

    # Reports exist and the repair plan is advisory only.
    assert (example_project / ".veritas" / "report.md").is_file()
    plan = json.loads(
        (
            example_project / ".veritas" / "runs" / _latest(example_project) / "repair-plan.json"
        ).read_text()
    )
    assert plan["actions"]
    assert plan["gate"] == "FAIL"


def test_the_artifact_is_never_modified(example_project: Path, repo_root: Path) -> None:
    """Evaluation must not touch what it evaluates."""
    before = {
        path.relative_to(example_project).as_posix(): path.read_bytes()
        for path in sorted(example_project.rglob("*"))
        if path.is_file()
    }
    runner.invoke(app, ["evaluate", str(example_project)])
    after = {
        path.relative_to(example_project).as_posix(): path.read_bytes()
        for path in sorted(example_project.rglob("*"))
        if path.is_file() and ".veritas" not in path.parts
    }
    for name, content in after.items():
        assert before[name] == content, f"{name} was modified during evaluation"


def _latest(project: Path) -> str:
    return (project / ".veritas" / "runs" / "latest").read_text().strip()
