"""Presentation: process narration, repeated hashes, abstract shape, figures."""

from __future__ import annotations

from pathlib import Path

import pytest

from veritas.artifacts.base import Artifact
from veritas.checks import build_check
from veritas.config import CheckConfig, ExecutionConfig

pytestmark = pytest.mark.asyncio

HASH = "378aabee42a69c61edc7d7a37c934465b4a66e30"
CLEAN = """# Abstract

We show one result, clearly.

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
    text = CLEAN.replace("We show one result, clearly.", "One.\n\nTwo.")
    assert titles(await run(tmp_path, text)) == ["Abstract is 2 paragraphs"]
    long = CLEAN.replace("We show one result, clearly.", "word " * 320)
    assert titles(await run(tmp_path, long))[0].startswith("Abstract is 3")


async def test_figures_missing_or_never_cited(tmp_path: Path) -> None:
    uncited = CLEAN.replace("Figure @fig:main shows it.", "It is shown.")
    assert titles(await run(tmp_path, uncited)) == ["Figure never referenced in the text: fig:main"]
    none = "# Abstract\n\nOne result.\n\n# Results\n\nText only.\n"
    assert titles(await run(tmp_path, none)) == ["The manuscript has no figure"]
    assert (await run(tmp_path, none, require_figures=False)).status == "pass"


async def test_latex_figures_are_understood(tmp_path: Path) -> None:
    text = (
        "\\begin{abstract}One.\\end{abstract}\n"
        "\\begin{figure}\\includegraphics{a}\\caption{A}\\label{fig:a}\\end{figure}\n"
        "See Figure~\\ref{fig:a}.\n"
    )
    assert (await run(tmp_path, text)).status == "pass"
