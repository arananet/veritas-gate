"""Read-only investigation: facts with sources, nothing changed, facts reach the repair."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from tests.test_loop import make_loop, passing, repairable, revise
from tests.test_repair_agents import plan_with

from veritas.config import RepairAgentConfig
from veritas.models.investigation import Fact, Investigation
from veritas.repair import MockRepairAgent, open_workspace
from veritas.repair.cli_agent import GenericCLIRepairAgent

# A stand-in for a CLI agent: reads the evidence, writes facts, and (wrongly)
# edits the manuscript and the evidence too.
INVESTIGATOR = """
import json, sys, pathlib
result = pathlib.Path(sys.argv[1])
rounds = json.load(open("evidence/run.json"))["rounds"]
pathlib.Path("doc.md").write_text("tampered\\n")
pathlib.Path("evidence/run.json").write_text("{}")
result.write_text(json.dumps({
    "facts": [
        {"statement": f"The run has {rounds} scored rounds", "source": "evidence/run.json:1",
         "action_id": "ACTION-001"},
        {"statement": "An unsourced claim", "source": ""},
    ],
    "unresolved": ["whether a tenth scenario existed"],
}))
"""


def project(tmp_path: Path) -> Path:
    source = tmp_path / "src"
    (source / "evidence").mkdir(parents=True)
    (source / "evidence" / "run.json").write_text('{"rounds": 350}', encoding="utf-8")
    (source / "doc.md").write_text("# Doc\n", encoding="utf-8")
    (source / "investigate.py").write_text(INVESTIGATOR, encoding="utf-8")
    return source


def agent() -> GenericCLIRepairAgent:
    config = RepairAgentConfig(
        provider="generic-cli", command=[sys.executable, "investigate.py", "{result_file}"]
    )
    return GenericCLIRepairAgent(config, frozen=["evidence/**"])


@pytest.mark.asyncio
async def test_facts_are_kept_with_sources_and_every_change_is_reverted(tmp_path: Path) -> None:
    workspace = open_workspace(project(tmp_path), "copy", loop_id="l", base_dir=tmp_path / "ws")
    investigation = await agent().investigate(plan_with(), workspace)

    assert [fact.statement for fact in investigation.facts] == ["The run has 350 scored rounds"]
    assert investigation.unresolved == ["whether a tenth scenario existed"]
    assert sorted(investigation.reverted) == ["doc.md", "evidence/run.json"]
    assert (workspace.root / "doc.md").read_text() == "# Doc\n"
    assert json.loads((workspace.root / "evidence" / "run.json").read_text()) == {"rounds": 350}


def test_the_investigation_prompt_forbids_changes_and_asks_for_sources(tmp_path: Path) -> None:
    prompt = agent().investigation_prompt(plan_with(), tmp_path / "out.json")
    assert "Do NOT modify" in prompt
    assert "Every fact must name its source" in prompt
    assert "Frozen files" in prompt
    assert "ACTION-001" in prompt


def test_verified_facts_reach_the_repair_prompt(tmp_path: Path) -> None:
    repairer = agent()
    repairer.facts = [Fact(statement="350 = 8x35 + 35x2", source="python3 -c ... -> 350")]
    prompt = repairer.render_prompt(plan_with(), tmp_path / "p.json", tmp_path / "r.json")
    assert "## Verified facts" in prompt
    assert "350 = 8x35 + 35x2 (source: python3 -c ... -> 350)" in prompt


class InvestigatingAgent(MockRepairAgent):
    def __init__(self) -> None:
        super().__init__()
        self.facts: list[Fact] = []
        self.facts_at_repair: list[Fact] = []

    async def investigate(self, plan, workspace):  # type: ignore[no-untyped-def]
        return Investigation(facts=[Fact(statement="135 = 80 + 55", source="git show x")])

    async def repair(self, artifact, plan, workspace):  # type: ignore[no-untyped-def]
        self.facts_at_repair = list(self.facts)
        return await super().repair(artifact, plan, workspace)


@pytest.mark.asyncio
async def test_the_loop_investigates_before_repairing(tmp_path: Path) -> None:
    repairer = InvestigatingAgent()
    orchestrator, workspace, artifact = make_loop(
        tmp_path, [revise([repairable()]), passing()], agent=repairer
    )
    await orchestrator.run(artifact, workspace)
    assert [fact.statement for fact in repairer.facts_at_repair] == ["135 = 80 + 55"]
    saved = next((tmp_path / "loops").rglob("investigation.json"))
    assert json.loads(saved.read_text())["facts"][0]["source"] == "git show x"
