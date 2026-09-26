"""Derived files: built with provenance, reported when stale or hand-edited."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from veritas.artifacts.base import Artifact
from veritas.checks import build_check
from veritas.config import CheckConfig, ConfigError, Derivation, ExecutionConfig
from veritas.derive import LOCK_NAME, build

SCRIPT = """import json, sys
data = json.load(open("evidence/run.json"))
open("figures/plot.svg", "w").write(f"<svg>{data['value']}</svg>")
"""


def project(tmp_path: Path) -> tuple[list[Derivation], ExecutionConfig]:
    (tmp_path / "evidence").mkdir()
    (tmp_path / "evidence" / "run.json").write_text('{"value": 1}', encoding="utf-8")
    (tmp_path / "figures").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "plot.py").write_text(SCRIPT, encoding="utf-8")
    (tmp_path / "paper.md").write_text("![Plot](figures/plot.svg)\n", encoding="utf-8")
    derivation = Derivation(
        id="plot",
        kind="figure",
        command=[sys.executable, "scripts/plot.py"],
        inputs=["evidence/run.json", "scripts/plot.py"],
        outputs=["figures/plot.svg"],
    )
    return [derivation], ExecutionConfig(allow=[sys.executable])


def check(tmp_path: Path, derived: list[Derivation]):
    config = CheckConfig(type="derived-freshness", target="paper.md", severity_on_failure="major")
    runner = build_check("derived", config, ExecutionConfig(), derived)
    return runner.run(Artifact(id="a", type="paper", root=tmp_path, paths=["."]))


def titles(result) -> list[str]:
    return [finding.title for finding in result.findings]


@pytest.mark.asyncio
async def test_a_built_derivation_is_fresh(tmp_path: Path) -> None:
    derived, execution = project(tmp_path)
    [result] = build(tmp_path, derived, execution, frozen=["evidence/**"])
    assert result.ok, result.detail
    assert (tmp_path / LOCK_NAME).is_file()
    assert (await check(tmp_path, derived)).status == "pass"


@pytest.mark.asyncio
async def test_a_changed_input_makes_the_output_stale(tmp_path: Path) -> None:
    derived, execution = project(tmp_path)
    build(tmp_path, derived, execution, frozen=[])
    (tmp_path / "scripts" / "plot.py").write_text(SCRIPT + "# tweak\n", encoding="utf-8")
    assert titles(await check(tmp_path, derived)) == [
        "Stale figure: 'plot' inputs changed since it was built"
    ]


@pytest.mark.asyncio
async def test_a_hand_edited_output_is_reported(tmp_path: Path) -> None:
    derived, execution = project(tmp_path)
    build(tmp_path, derived, execution, frozen=[])
    (tmp_path / "figures" / "plot.svg").write_text("<svg>touched</svg>", encoding="utf-8")
    assert titles(await check(tmp_path, derived)) == [
        "Derived figure edited by hand: figures/plot.svg"
    ]


@pytest.mark.asyncio
async def test_never_built_and_figures_without_provenance(tmp_path: Path) -> None:
    derived, _ = project(tmp_path)
    (tmp_path / "figures" / "plot.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "figures" / "drawn.png").write_bytes(b"png")
    (tmp_path / "paper.md").write_text(
        "![Plot](figures/plot.svg)\n![Hand](figures/drawn.png)\n", encoding="utf-8"
    )
    assert titles(await check(tmp_path, derived)) == [
        "Derivation 'plot' was never built through Veritas",
        "Figure without provenance: figures/drawn.png",
    ]


def test_building_requires_the_allow_list(tmp_path: Path) -> None:
    derived, _ = project(tmp_path)
    with pytest.raises(ConfigError, match=r"execution\.allow"):
        build(tmp_path, derived, ExecutionConfig(), frozen=[])


def test_a_derivation_may_not_write_frozen_evidence(tmp_path: Path) -> None:
    derived, execution = project(tmp_path)
    derived[0].outputs = ["evidence/summary.json"]
    with pytest.raises(ConfigError, match="frozen evidence"):
        build(tmp_path, derived, execution, frozen=["evidence/**"])


def test_a_script_that_rewrites_evidence_is_rejected(tmp_path: Path) -> None:
    derived, execution = project(tmp_path)
    (tmp_path / "scripts" / "plot.py").write_text(
        SCRIPT + 'open("evidence/run.json", "w").write("{}")\n', encoding="utf-8"
    )
    [result] = build(tmp_path, derived, execution, frozen=["evidence/**"])
    assert not result.ok
    assert "frozen evidence" in result.detail
    assert (tmp_path / "evidence" / "run.json").read_text(encoding="utf-8") == '{"value": 1}'
