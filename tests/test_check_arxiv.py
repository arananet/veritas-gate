"""arXiv readiness: the source checked the way arXiv will receive it."""

from __future__ import annotations

from pathlib import Path

import pytest

from veritas.artifacts.base import Artifact
from veritas.checks import build_check
from veritas.config import CheckConfig, ExecutionConfig

pytestmark = pytest.mark.asyncio

MAIN = r"""\documentclass{article}
\graphicspath{{figures/}}
\begin{document}
\input{sections/intro}
\includegraphics[width=\linewidth]{plot}
See \cite{known, unknown}. 50\% of runs. % TODO: reviewer 2 is right
\bibliography{refs}
\end{document}
"""

BIB = """@article{known,
  author = {Doe, Jane},
  title = {A Title},
  year = {2025},
  doi = {not-a-doi}
}
@misc{unused, title = {Never cited}}
"""


def paper(tmp_path: Path, main: str = MAIN, *, bbl: bool = False) -> Artifact:
    root = tmp_path / "paper"
    (root / "sections").mkdir(parents=True)
    (root / "figures").mkdir()
    (root / "main.tex").write_text(main, encoding="utf-8")
    (root / "sections" / "intro.tex").write_text("Intro text.\n", encoding="utf-8")
    (root / "figures" / "plot.pdf").write_bytes(b"%PDF")
    (root / "refs.bib").write_text(BIB, encoding="utf-8")
    if bbl:
        (root / "main.bbl").write_text("", encoding="utf-8")
    return Artifact(id="a", type="paper", root=tmp_path, paths=["paper"])


def check(**kwargs: object):
    return build_check(
        "arxiv",
        CheckConfig(type="arxiv-package", **kwargs),
        ExecutionConfig(),  # type: ignore[arg-type]
    )


def titles(result) -> list[str]:
    return [finding.title for finding in result.findings]


async def test_the_main_file_is_found_and_problems_are_reported(tmp_path: Path) -> None:
    result = await check().run(paper(tmp_path))
    found = " | ".join(titles(result))
    assert result.status == "fail"
    assert "No main.bbl" in found
    assert "1 citation key(s) have no bibliography entry" in found
    assert "1 DOI(s) are malformed" in found
    assert "1 comment(s) look private" in found
    # graphicspath, \input without suffix and the escaped \% are all handled.
    assert "Missing" not in found
    unknown = next(f for f in result.findings if "citation key" in f.title)
    assert unknown.evidence == ["unknown"]


async def test_a_clean_package_passes(tmp_path: Path) -> None:
    main = MAIN.replace(", unknown", "").replace("% TODO: reviewer 2 is right", "")
    artifact = paper(tmp_path, main, bbl=True)
    (tmp_path / "paper" / "refs.bib").write_text(
        BIB.replace("not-a-doi", "https://doi.org/10.1234/abc"), encoding="utf-8"
    )
    result = await check().run(artifact)
    assert result.status == "pass", titles(result)


async def test_missing_inputs_figures_and_bibliography_are_major(tmp_path: Path) -> None:
    main = MAIN.replace("sections/intro", "sections/gone").replace("{plot}", "{nofig}")
    main = main.replace("{refs}", "{nobib}")
    result = await check(severity_on_failure="major").run(paper(tmp_path, main, bbl=True))
    missing = [f for f in result.findings if f.title.startswith("Missing")]
    assert {f.title for f in missing} == {
        "Missing input: sections/gone",
        "Missing figure: nofig",
        "Missing bibliography: nobib",
    }
    assert all(f.severity == "major" for f in missing)
    assert any(f.location == "paper/main.tex:4" for f in missing)


async def test_the_size_limit(tmp_path: Path) -> None:
    artifact = paper(tmp_path, bbl=True)
    (tmp_path / "paper" / "figures" / "big.png").write_bytes(b"0" * 2_000_000)
    result = await check(max_package_mb=1).run(artifact)
    assert any("arXiv accepts at most 1 MB" in title for title in titles(result))


async def test_no_latex_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "paper.md").write_text("# Paper\n", encoding="utf-8")
    artifact = Artifact(id="a", type="paper", root=tmp_path, paths=["paper.md"])
    assert (await check().run(artifact)).status == "skipped"


async def test_several_main_files_ask_for_a_target(tmp_path: Path) -> None:
    artifact = paper(tmp_path, bbl=True)
    (tmp_path / "paper" / "old.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    result = await check().run(artifact)
    assert result.status == "skipped"
    assert "set target" in result.summary
    targeted = await check(target="paper/main.tex").run(artifact)
    assert targeted.status != "skipped"
