"""Numeric traceability: every number in the paper comes from the evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from veritas.artifacts.base import Artifact
from veritas.checks import build_check
from veritas.config import CheckConfig, ExecutionConfig

pytestmark = pytest.mark.asyncio


def run(tmp_path: Path, manuscript: str, evidence: dict, **kwargs: object):
    (tmp_path / "paper.md").write_text(manuscript, encoding="utf-8")
    (tmp_path / "evidence").mkdir(exist_ok=True)
    (tmp_path / "evidence" / "run.json").write_text(json.dumps(evidence), encoding="utf-8")
    config = CheckConfig(
        type="numeric-traceability",
        target="paper.md",
        sources=["evidence"],
        severity_on_failure="minor",
        **kwargs,  # type: ignore[arg-type]
    )
    check = build_check("numbers", config, ExecutionConfig())
    return check.run(Artifact(id="a", type="paper", root=tmp_path, paths=["."]))


def titles(result) -> list[str]:
    return [finding.title for finding in result.findings]


async def test_numbers_in_the_evidence_are_traced(tmp_path: Path) -> None:
    result = await run(
        tmp_path,
        "We scored 350 rounds and observed 135 gate calls, with a rate of 0.3857.\n",
        {"rounds": 350, "gate_calls": 135, "rate": 0.385714},
    )
    assert result.status == "pass", titles(result)


async def test_a_number_nowhere_in_the_evidence_is_reported(tmp_path: Path) -> None:
    result = await run(tmp_path, "The audit covered 315 executions.\n", {"rounds": 350})
    assert titles(result) == ["Number not traceable to the evidence: 315"]
    assert result.findings[0].location == "paper.md:1"


async def test_percentages_and_stated_ratios_are_traced(tmp_path: Path) -> None:
    text = "Of 350 rounds, 52 (14.9%) remain. Gated rounds: 100, at 1.35 calls each.\n"
    result = await run(tmp_path, text, {"total": 350, "unresolved": 52, "gated": 100, "calls": 135})
    assert result.status == "pass", titles(result)


async def test_a_ratio_needs_a_denominator_the_paper_states(tmp_path: Path) -> None:
    # 0.37 = 74/200, but the paper never names 200: that is not a trace.
    result = await run(tmp_path, "The rate was 0.37.\n", {"a": 74, "b": 200})
    assert titles(result) == ["Number not traceable to the evidence: 0.37"]


async def test_identifiers_dates_versions_and_small_counts_are_ignored(tmp_path: Path) -> None:
    text = (
        "Run `20260713T191740Z` on 13 July 2026 used v0.3.2 and commit 46ab528, "
        "in Section 5 and Table 2, with three models over 5 repetitions. "
        "See https://doi.org/10.5281/zenodo.22970417 and CB-VAL-003.\n"
    )
    result = await run(tmp_path, text, {"x": 1})
    assert result.status == "pass", titles(result)


async def test_ignore_numbers_leaves_parameters_alone(tmp_path: Path) -> None:
    result = await run(tmp_path, "Context was 128 tokens.\n", {"x": 1}, ignore_numbers=["128"])
    assert result.status == "pass"


async def test_no_sources_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "paper.md").write_text("350\n", encoding="utf-8")
    config = CheckConfig(type="numeric-traceability", target="paper.md")
    check = build_check("numbers", config, ExecutionConfig())
    result = await check.run(Artifact(id="a", type="paper", root=tmp_path, paths=["."]))
    assert result.status == "skipped"
