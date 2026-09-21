"""Deterministic checks, including allow-list enforcement."""

from __future__ import annotations

import sys
from pathlib import Path

from veritas.artifacts import Artifact
from veritas.checks import build_check
from veritas.config import CheckConfig, ExecutionConfig


def artifact_at(root: Path) -> Artifact:
    return Artifact(id="a", type="document", root=root, paths=["."])


async def test_command_is_refused_when_not_allow_listed(tmp_path: Path) -> None:
    check = build_check(
        "tests",
        CheckConfig(type="command", command=["python", "-c", "print(1)"]),
        ExecutionConfig(allow=[]),
    )
    result = await check.run(artifact_at(tmp_path))
    assert result.status == "error"
    assert "not in execution.allow" in result.summary
    assert result.findings[0].severity == "info"


async def test_allow_listed_command_runs(tmp_path: Path) -> None:
    check = build_check(
        "ok",
        CheckConfig(type="command", command=[sys.executable, "-c", "print('hello')"]),
        ExecutionConfig(allow=[sys.executable, Path(sys.executable).name]),
    )
    result = await check.run(artifact_at(tmp_path))
    assert result.status == "pass"
    assert "hello" in result.metadata["output"]


async def test_failing_command_becomes_a_finding(tmp_path: Path) -> None:
    check = build_check(
        "failing",
        CheckConfig(
            type="command",
            command=[sys.executable, "-c", "import sys; print('boom'); sys.exit(2)"],
            severity_on_failure="critical",
        ),
        ExecutionConfig(allow=[sys.executable, Path(sys.executable).name]),
    )
    result = await check.run(artifact_at(tmp_path))
    assert result.status == "fail"
    assert result.findings[0].severity == "critical"
    assert result.findings[0].confidence == 1.0
    assert "boom" in " ".join(result.findings[0].evidence)


async def test_command_timeout_is_a_failure_not_a_hang(tmp_path: Path) -> None:
    check = build_check(
        "slow",
        CheckConfig(
            type="command",
            command=[sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.5,
        ),
        ExecutionConfig(allow=[sys.executable, Path(sys.executable).name]),
    )
    result = await check.run(artifact_at(tmp_path))
    assert result.status == "fail"
    assert "timed out" in result.summary


async def test_environment_is_not_inherited_by_default(tmp_path: Path) -> None:
    import os

    os.environ["VERITAS_TEST_SECRET"] = "leaked"
    try:
        check = build_check(
            "env",
            CheckConfig(
                type="command",
                command=[
                    sys.executable,
                    "-c",
                    "import os; print(os.environ.get('VERITAS_TEST_SECRET', 'absent'))",
                ],
            ),
            ExecutionConfig(allow=[sys.executable, Path(sys.executable).name]),
        )
        result = await check.run(artifact_at(tmp_path))
        assert "absent" in result.metadata["output"]
    finally:
        del os.environ["VERITAS_TEST_SECRET"]


async def test_required_paths_check(tmp_path: Path) -> None:
    (tmp_path / "present.md").write_text("x")
    check = build_check(
        "structure",
        CheckConfig(type="required-paths", required_paths=["present.md", "absent.md"]),
        ExecutionConfig(),
    )
    result = await check.run(artifact_at(tmp_path))
    assert result.status == "fail"
    assert len(result.findings) == 1
    assert result.findings[0].location == "absent.md"


async def test_required_sections_check(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("# Abstract\n\ntext\n\n# Results\n\nmore")
    check = build_check(
        "sections",
        CheckConfig(
            type="required-sections",
            target="doc.md",
            required_sections=["abstract", "results", "conclusion"],
        ),
        ExecutionConfig(),
    )
    result = await check.run(artifact_at(tmp_path))
    assert result.status == "fail"
    assert [item.title for item in result.findings] == ["Missing required section: conclusion"]


async def test_check_with_nothing_configured_is_skipped(tmp_path: Path) -> None:
    check = build_check("empty", CheckConfig(type="required-paths"), ExecutionConfig())
    result = await check.run(artifact_at(tmp_path))
    assert result.status == "skipped"
    assert result.passed
