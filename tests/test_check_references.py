"""Reference integrity: does what a document cites actually exist?"""

from __future__ import annotations

from pathlib import Path

import pytest

from veritas.artifacts.base import Artifact
from veritas.checks import build_check
from veritas.config import CheckConfig, ExecutionConfig

pytestmark = pytest.mark.asyncio


def make(tmp_path: Path, manuscript: str, *, paths: list[str] | None = None) -> Artifact:
    (tmp_path / "paper").mkdir(parents=True, exist_ok=True)
    (tmp_path / "paper" / "manuscript.md").write_text(manuscript, encoding="utf-8")
    return Artifact(
        id="a",
        type="paper",
        root=tmp_path,
        paths=paths if paths is not None else ["paper/manuscript.md"],
    )


def check(target: str = "paper/manuscript.md"):
    return build_check(
        "references",
        CheckConfig(type="reference-integrity", target=target, severity_on_failure="major"),
        ExecutionConfig(),
    )


async def test_a_resolvable_link_reports_nothing(tmp_path: Path) -> None:
    (tmp_path / "paper" / "evidence").mkdir(parents=True)
    (tmp_path / "paper" / "evidence" / "run.json").write_text("{}", encoding="utf-8")
    artifact = make(
        tmp_path,
        "See the [run report](./evidence/run.json).\n",
        paths=["paper/manuscript.md", "paper/evidence"],
    )
    result = await check().run(artifact)
    assert result.status == "pass"
    assert result.findings == []


async def test_a_path_that_exists_nowhere_is_reported_with_its_line(tmp_path: Path) -> None:
    """The bug that eight LLM judges found three times over."""
    artifact = make(
        tmp_path,
        "Intro.\nSee the [run report](./evidence/browser-s2-fixed-order-QDL9iC9j/run.json).\n",
    )
    result = await check().run(artifact)

    assert result.status == "fail"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert "QDL9iC9j" in finding.title
    assert finding.location == "paper/manuscript.md:2"
    assert "exists nowhere" in finding.description


async def test_a_path_on_disk_but_outside_artifact_paths_is_a_configuration_finding(
    tmp_path: Path,
) -> None:
    """Not the same problem as a dangling reference, and must not read like one.

    Conflated, it sends an author rewriting correct prose to match a truth
    Veritas could not see.
    """
    (tmp_path / "paper" / "evidence").mkdir(parents=True)
    (tmp_path / "paper" / "evidence" / "run.json").write_text("{}", encoding="utf-8")
    artifact = make(
        tmp_path,
        "See the [run report](./evidence/run.json).\n",
        paths=["paper/manuscript.md"],  # evidence deliberately not supplied
    )
    result = await check().run(artifact)

    assert result.status == "fail"
    finding = result.findings[0]
    assert "not supplied" in finding.title
    assert "configuration is incomplete" in finding.description
    assert "artifact.paths" in (finding.recommendation or "")
    assert "paper/evidence/run.json" in (finding.recommendation or "")


async def test_external_urls_are_ignored(tmp_path: Path) -> None:
    artifact = make(
        tmp_path,
        "See [the repo](https://github.com/example/thing) and [mail](mailto:a@b.c).\n",
    )
    result = await check().run(artifact)
    assert result.status == "pass"


async def test_fragments_are_ignored(tmp_path: Path) -> None:
    artifact = make(tmp_path, "See [the methods](#methodology).\n")
    result = await check().run(artifact)
    assert result.status == "pass"


async def test_a_directory_target_resolves(tmp_path: Path) -> None:
    (tmp_path / "paper" / "evidence" / "run").mkdir(parents=True)
    artifact = make(
        tmp_path,
        "Evidence lives in [this directory](./evidence/run).\n",
        paths=["paper/manuscript.md", "paper/evidence"],
    )
    result = await check().run(artifact)
    assert result.status == "pass"


async def test_a_path_in_inline_code_is_checked(tmp_path: Path) -> None:
    artifact = make(tmp_path, "The report is at `paper/evidence/missing/run.json`.\n")
    result = await check().run(artifact)
    assert result.status == "fail"
    assert "missing/run.json" in result.findings[0].title


async def test_prose_in_inline_code_is_not_mistaken_for_a_path(tmp_path: Path) -> None:
    artifact = make(tmp_path, "We call `outcomeOf` and then `git status` to inspect.\n")
    result = await check().run(artifact)
    assert result.status == "pass"


async def test_a_link_with_an_anchor_resolves_to_the_file(tmp_path: Path) -> None:
    (tmp_path / "paper").mkdir(parents=True, exist_ok=True)
    (tmp_path / "paper" / "REPRODUCTION.md").write_text("# Steps\n", encoding="utf-8")
    artifact = make(
        tmp_path,
        "See [the steps](./REPRODUCTION.md#inputs).\n",
        paths=["paper/manuscript.md", "paper/REPRODUCTION.md"],
    )
    result = await check().run(artifact)
    assert result.status == "pass"


async def test_no_configured_document_is_skipped_not_passed(tmp_path: Path) -> None:
    artifact = make(tmp_path, "text\n")
    result = await build_check(
        "references", CheckConfig(type="reference-integrity"), ExecutionConfig()
    ).run(artifact)
    assert result.status == "skipped"


async def test_an_unreadable_target_is_an_error_not_a_pass(tmp_path: Path) -> None:
    artifact = make(tmp_path, "")
    result = await check().run(artifact)
    assert result.status == "error"


async def test_the_configured_severity_is_used(tmp_path: Path) -> None:
    artifact = make(tmp_path, "See [gone](./nowhere.json).\n")
    checker = build_check(
        "references",
        CheckConfig(
            type="reference-integrity", target="paper/manuscript.md", severity_on_failure="minor"
        ),
        ExecutionConfig(),
    )
    result = await checker.run(artifact)
    assert result.findings[0].severity == "minor"


async def test_a_root_relative_path_in_prose_resolves(tmp_path: Path) -> None:
    """Both spellings appear in one document and both must resolve.

    A Markdown link is written relative to the file it sits in; a path quoted
    in prose is usually written from the repository root. Trying only the
    first reported `paper/evidence/run` as missing from `paper/manuscript.md`,
    because it resolved to `paper/paper/evidence/run`.
    """
    (tmp_path / "paper" / "evidence" / "run").mkdir(parents=True)
    artifact = make(
        tmp_path,
        "Prior run in `paper/evidence/run`.\nSee also [it](./evidence/run).\n",
        paths=["paper/manuscript.md", "paper/evidence"],
    )
    result = await check().run(artifact)
    assert result.status == "pass", [f.title for f in result.findings]


async def test_a_path_missing_under_both_spellings_is_still_reported(tmp_path: Path) -> None:
    artifact = make(tmp_path, "Prior run in `paper/evidence/gone`.\n")
    result = await check().run(artifact)
    assert result.status == "fail"
    assert "gone" in result.findings[0].title


# ------------------------------------------- prose is not a path
#
# Every case below was a false positive against a real manuscript. A check
# that cries wolf is worse than no check: it sends a repair agent chasing
# ghosts and buries the findings that matter.


@pytest.mark.parametrize(
    "token",
    [
        "0.21.0",  # a package version in a table
        "2.0.0",
        "^19.2.3",
        "Tracer.record",  # a dotted identifier
        "outcomeOf",
        "size10.clo",  # LaTeX package names, not repository files
        "textcomp.sty",
        "calc.sty",
    ],
)
async def test_a_dotted_token_in_prose_is_not_a_path(tmp_path: Path, token: str) -> None:
    artifact = make(tmp_path, f"The value is `{token}` in this configuration.\n")
    result = await check().run(artifact)
    assert result.status == "pass", [f.title for f in result.findings]


async def test_an_upstream_owner_and_repository_is_not_a_local_path(tmp_path: Path) -> None:
    """`google/A2UI` is an upstream project, not a directory here."""
    artifact = make(tmp_path, "The inspected `google/A2UI` URL redirects to the project.\n")
    result = await check().run(artifact)
    assert result.status == "pass", [f.title for f in result.findings]


async def test_a_real_repository_path_in_inline_code_is_still_checked(tmp_path: Path) -> None:
    (tmp_path / "paper" / "evidence").mkdir(parents=True, exist_ok=True)
    artifact = make(tmp_path, "The run is in `paper/evidence/gone`.\n")
    result = await check().run(artifact)
    assert result.status == "fail"
    assert "gone" in result.findings[0].title


# ------------------------------------------- one fix, one finding


async def test_citations_to_one_unsupplied_path_are_grouped(tmp_path: Path) -> None:
    """Thirty findings once described four edits to artifact.paths."""
    (tmp_path / "paper" / "evidence").mkdir(parents=True)
    (tmp_path / "paper" / "evidence" / "run.json").write_text("{}", encoding="utf-8")
    artifact = make(
        tmp_path,
        "See [it](./evidence/run.json).\n"
        "Again [here](./evidence/run.json).\n"
        "And [once more](./evidence/run.json).\n",
        paths=["paper/manuscript.md"],
    )
    result = await check().run(artifact)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert "3 citations" in finding.description
    assert len(finding.evidence) == 3


async def test_separate_unsupplied_paths_stay_separate(tmp_path: Path) -> None:
    (tmp_path / "paper" / "evidence").mkdir(parents=True)
    (tmp_path / "paper" / "evidence" / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "paper" / "evidence" / "b.json").write_text("{}", encoding="utf-8")
    artifact = make(
        tmp_path,
        "See [a](./evidence/a.json) and [b](./evidence/b.json).\n",
        paths=["paper/manuscript.md"],
    )
    result = await check().run(artifact)
    assert len(result.findings) == 2
