"""Withheld evidence: named to judges, never sent, never reported as absent."""

from __future__ import annotations

from pathlib import Path

from veritas.artifacts import Artifact
from veritas.judges.base import EvaluationContext
from veritas.judges.llm import LLMJudge
from veritas.withheld import MAX_LISTED, resolve_withheld, withheld_section


def _tree(root: Path, count: int = 3) -> None:
    (root / "evidence" / "run").mkdir(parents=True)
    for i in range(count):
        (root / "evidence" / "run" / f"CB-{i:03}.json").write_text("{}", encoding="utf-8")
    (root / "evidence" / "summary.json").write_text("{}", encoding="utf-8")


def test_patterns_resolve_to_files_and_unmatched_ones_are_ignored(tmp_path: Path) -> None:
    _tree(tmp_path)
    found = resolve_withheld(tmp_path, ["evidence/run/CB-*", "nothing/**"], set())
    assert found == [f"evidence/run/CB-{i:03}.json" for i in range(3)]


def test_a_directory_pattern_expands(tmp_path: Path) -> None:
    _tree(tmp_path)
    assert len(resolve_withheld(tmp_path, ["evidence"], set())) == 4


def test_a_supplied_path_is_not_withheld(tmp_path: Path) -> None:
    _tree(tmp_path)
    found = resolve_withheld(tmp_path, ["evidence/**/*.json"], {"evidence/summary.json"})
    assert "evidence/summary.json" not in found
    assert len(found) == 3


def test_the_listing_is_bounded_and_counts_the_rest(tmp_path: Path) -> None:
    _tree(tmp_path, MAX_LISTED + 10)
    paths = resolve_withheld(tmp_path, ["evidence/run"], set())
    section = withheld_section(paths, "too large for the token limit")
    assert section.count("  - evidence/run/CB-") == MAX_LISTED
    assert "and 10 more under evidence/run/" in section
    assert "too large for the token limit" in section
    assert "not a defect" in section


def _judge() -> LLMJudge:
    return LLMJudge(name="evidence", prompt="Judge evidence.", provider=None)  # type: ignore[arg-type]


def _context(**kwargs: object) -> EvaluationContext:
    return EvaluationContext(
        profile="p", profile_version="1", run_id="r", artifact_type="document", **kwargs
    )  # type: ignore[arg-type]


def test_the_judge_prompt_names_withheld_files_only_when_set() -> None:
    judge = _judge()
    with_section = judge.system_prompt(_context(withheld=["evidence/run/CB-000.json"]))
    assert "WITHHELD FILES (1)" in with_section
    assert "evidence/run/CB-000.json" in with_section
    assert "WITHHELD FILES" not in judge.system_prompt(_context())


def test_artifact_does_not_read_withheld_files(tmp_path: Path) -> None:
    _tree(tmp_path)
    artifact = Artifact(id="a", type="document", root=tmp_path, paths=["evidence/summary.json"])
    assert artifact.file_list() == ["evidence/summary.json"]
