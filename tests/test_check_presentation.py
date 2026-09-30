"""Presentation: process narration, repeated hashes, abstract shape, figures."""

from __future__ import annotations

from pathlib import Path

import pytest

from veritas.artifacts.base import Artifact
from veritas.checks import build_check
from veritas.config import CheckConfig, ExecutionConfig

pytestmark = pytest.mark.asyncio

RESULT = "We show one result, clearly: accuracy rises from 61% to 74%."
HASH = "378aabee42a69c61edc7d7a37c934465b4a66e30"
CLEAN = """# Abstract

We show one result, clearly: accuracy rises from 61% to 74%.

# Results

Figure @fig:main shows it.

![Main result](figures/main.pdf){#fig:main}
"""


def run(tmp_path: Path, text: str, **kwargs: object):
    (tmp_path / "paper.md").write_text(text, encoding="utf-8")
    config = CheckConfig(type="presentation", target="paper.md", **kwargs)  # type: ignore[arg-type]
    check = build_check("presentation", config, ExecutionConfig())
    return check.run(Artifact(id="a", type="paper", root=tmp_path, paths=["."]))


def titles(result) -> list[str]:
    return [finding.title for finding in result.findings]


async def test_a_clean_manuscript_passes(tmp_path: Path) -> None:
    assert (await run(tmp_path, CLEAN)).status == "pass"


async def test_process_narration_is_major(tmp_path: Path) -> None:
    text = CLEAN + (
        "\nHuman scientific and editorial review remains pending. This session is an\n"
        "agent-assisted local rerun. The build failed with EPERM; 30 passed and 3 failed.\n"
    )
    result = await run(tmp_path, text, severity_on_failure="major")
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.severity == "major"
    assert finding.title.startswith("Process narration in the manuscript")
    assert len(finding.evidence) == 2  # one entry per line


async def test_a_hash_repeated_in_prose_but_not_in_urls(tmp_path: Path) -> None:
    prose = CLEAN + f"\nCommit `{HASH}`. Again {HASH}. And {HASH}.\n"
    assert titles(await run(tmp_path, prose)) == [
        "Long identifiers repeated throughout the manuscript"
    ]
    urls = CLEAN + "".join(f"\nhttps://example.org/tree/{HASH}/x{i}\n" for i in range(4))
    assert (await run(tmp_path, urls)).status == "pass"


async def test_a_multi_paragraph_or_long_abstract(tmp_path: Path) -> None:
    text = CLEAN.replace(RESULT, "One.\n\nTwo.")
    assert titles(await run(tmp_path, text, abstract_requires_number=False)) == [
        "Abstract is 2 paragraphs"
    ]
    long = CLEAN.replace(RESULT, "word " * 320)
    assert titles(await run(tmp_path, long))[0].startswith("Abstract is 3")


async def test_figures_missing_or_never_cited(tmp_path: Path) -> None:
    uncited = CLEAN.replace("Figure @fig:main shows it.", "It is shown.")
    assert titles(await run(tmp_path, uncited)) == ["Figure never referenced in the text: fig:main"]
    none = "# Abstract\n\nOne result, 12%.\n\n# Results\n\nText only.\n"
    assert titles(await run(tmp_path, none)) == ["The manuscript has no figure"]
    assert (await run(tmp_path, none, require_figures=False)).status == "pass"


async def test_latex_figures_are_understood(tmp_path: Path) -> None:
    text = (
        "\\begin{abstract}One, 2.5 points.\\end{abstract}\n"
        "\\begin{figure}\\includegraphics{a}\\caption{A}\\label{fig:a}\\end{figure}\n"
        "See Figure~\\ref{fig:a}.\n"
    )
    assert (await run(tmp_path, text)).status == "pass"


# Disclaimer density. Fixtures are real abstracts: two drafts that were flagged
# or desk-rejected, and one accepted preprint whose negatives are results.
ABSTRACTS = Path(__file__).parent / "fixtures" / "abstracts"
UI_ABSTRACT = (ABSTRACTS / "flagged_case_study.txt").read_text(encoding="utf-8")
CONTAM_ABSTRACT = (ABSTRACTS / "desk_rejected_benchmark.txt").read_text(encoding="utf-8")
ACCEPTED_ABSTRACT = (ABSTRACTS / "accepted_negative_results.txt").read_text(encoding="utf-8")


def with_abstract(body: str) -> str:
    return f"# Abstract\n\n{body}\n\n# Results\n\nFigure @fig:main.\n\n![M](m.pdf){{#fig:main}}\n"


def disclaimer_findings(result) -> list[str]:
    return [t for t in titles(result) if "does not show" in t]


async def test_crowded_abstracts_are_reported(tmp_path: Path) -> None:
    for body in (UI_ABSTRACT, CONTAM_ABSTRACT):
        result = await run(tmp_path, with_abstract(body), abstract_max_words=1000)
        assert disclaimer_findings(result), body[:40]


async def test_negative_results_are_not_disclaimers(tmp_path: Path) -> None:
    result = await run(tmp_path, with_abstract(ACCEPTED_ABSTRACT), abstract_max_words=1000)
    assert not disclaimer_findings(result)


async def test_disclaimer_finding_is_advisory(tmp_path: Path) -> None:
    result = await run(tmp_path, with_abstract(CONTAM_ABSTRACT), abstract_max_words=1000)
    finding = next(f for f in result.findings if "does not show" in f.title)
    assert finding.severity == "minor"
    assert "relocate" in finding.recommendation


async def test_disclaimer_ratio_is_configurable(tmp_path: Path) -> None:
    result = await run(
        tmp_path,
        with_abstract(UI_ABSTRACT),
        abstract_max_words=1000,
        abstract_max_disclaimer_ratio=0.5,
    )
    assert not disclaimer_findings(result)


async def test_short_abstract_with_one_scope_sentence_passes(tmp_path: Path) -> None:
    body = (
        "We find X. In the nine evaluated scenarios, namespaces excluded Y. "
        "The result does not establish real-world prevalence."
    )
    assert not disclaimer_findings(await run(tmp_path, with_abstract(body)))


async def test_users_description_is_conversation(tmp_path: Path) -> None:
    text = CLEAN + "\nThe user's description of that project motivated our questions.\n"
    assert any("conversation" in t for t in titles(await run(tmp_path, text)))


FRONT = """# Abstract

Our method helps agents.

# Introduction

Agents are useful. We study them.

# Related Work

Others studied agents.

Table 1: Hyperparameters.

# Results

Figure @fig:main shows it.

![Main result](figures/main.pdf){#fig:main}
"""


async def test_front_matter_gaps_are_reported(tmp_path: Path) -> None:
    result = await run(tmp_path, FRONT)
    assert titles(result) == [
        "Abstract reports no quantitative result",
        "Introduction does not list its contributions",
        "No table positions the work against prior work",
    ]
    assert all(f.severity == "minor" for f in result.findings)


async def test_front_matter_done_well_passes(tmp_path: Path) -> None:
    text = (
        FRONT.replace("Our method helps agents.", "Success rises from 39.3% to 58.4%.")
        .replace("We study them.", "We contribute:\n\n- a dataset\n- a model")
        .replace("Table 1: Hyperparameters.", "Table 1: Comparison with existing benchmarks.")
    )
    assert (await run(tmp_path, text)).status == "pass"


async def test_front_matter_checks_are_configurable(tmp_path: Path) -> None:
    result = await run(
        tmp_path,
        FRONT.replace("Agents are useful.", "Our contributions are two."),
        abstract_requires_number=False,
        require_positioning_table=False,
    )
    assert result.status == "pass"


async def test_late_first_figure_is_reported(tmp_path: Path) -> None:
    text = CLEAN.replace("# Results", "# Method\n\n" + "word " * 60 + "\n\n# Results")
    result = await run(tmp_path, text, first_figure_within_words=50)
    assert titles(result) == ["First figure appears after more than 50 words"]


async def test_latex_front_matter_is_understood(tmp_path: Path) -> None:
    text = (
        "\\begin{abstract}Gains of 19.1 points.\\end{abstract}\n"
        "\\section{Introduction}\n\\begin{itemize}\\item one\\end{itemize}\n"
        "\\section{Related Work}\nPrior.\n"
        "\\begin{table}\\caption{Comparison with prior work}\\end{table}\n"
        "\\begin{figure}\\label{fig:a}\\end{figure} See \\ref{fig:a}.\n"
    )
    assert (await run(tmp_path, text)).status == "pass"
