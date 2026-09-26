"""PDF inspection, venue rules and anonymized packaging."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from veritas.artifacts.base import Artifact
from veritas.checks import build_check
from veritas.config import CheckConfig, ExecutionConfig
from veritas.venue import identity_terms, package_anonymized

CFF = """cff-version: 1.2.0
authors:
- family-names: Doe
  given-names: Jane
  orcid: https://orcid.org/0000-0001-2345-6789
repository-code: https://github.com/janedoe/project
doi: 10.5281/zenodo.123
"""

HAS_TEX = shutil.which("pdflatex") is not None and shutil.which("pdftotext") is not None


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def project(tmp_path: Path, body: str, preamble: str = "\\usepackage{hyperref}") -> Path:
    (tmp_path / "CITATION.cff").write_text(CFF, encoding="utf-8")
    tex = (
        "\\documentclass{article}\n"
        + preamble
        + "\n\\begin{document}\n"
        + body
        + "\n\\end{document}\n"
    )
    (tmp_path / "main.tex").write_text(tex, encoding="utf-8")
    if HAS_TEX:
        subprocess.run(
            ["pdflatex", "-interaction=nonstopmode", "main.tex"],
            cwd=tmp_path,
            capture_output=True,
            check=False,
        )
    return tmp_path


def run(root: Path, config: CheckConfig):
    check = build_check(config.type, config, ExecutionConfig())
    import asyncio

    return asyncio.run(check.run(Artifact(id="a", type="paper", root=root, paths=["."])))


def titles(result) -> list[str]:
    return [finding.title for finding in result.findings]


def test_identity_terms_come_from_citation_and_remote(tmp_path: Path) -> None:
    (tmp_path / "CITATION.cff").write_text(CFF, encoding="utf-8")
    terms = identity_terms(tmp_path, ["Acme Corp"])
    for term in [
        "Jane Doe",
        "Jane",
        "Doe",
        "0000-0001-2345-6789",
        "janedoe",
        "10.5281/zenodo.123",
        "Acme Corp",
    ]:
        assert term in terms, term


@pytest.mark.skipif(not HAS_TEX, reason="needs pdflatex and poppler")
def test_a_double_blind_venue_finds_the_author(tmp_path: Path) -> None:
    root = project(tmp_path, "By Jane Doe. \\appendix Appendix. \\bibliography{x}")
    config = CheckConfig(type="venue", venue="tmlr", target="main.pdf", source="main.tex")
    found = titles(run(root, config))
    assert "tmlr: the submission identifies the author" in found
    assert "tmlr template not used" in found
    assert "Appendix comes before the references" in found
    assert "No broader impact statement" in found


@pytest.mark.skipif(not HAS_TEX, reason="needs pdflatex and poppler")
def test_arxiv_does_not_require_anonymity(tmp_path: Path) -> None:
    root = project(tmp_path, "By Jane Doe.")
    config = CheckConfig(type="venue", venue="arxiv", target="main.pdf", source="main.tex")
    assert run(root, config).status == "pass"


@pytest.mark.skipif(not HAS_TEX, reason="needs pdflatex and poppler")
def test_pdf_inspection_reports_unresolved_references(tmp_path: Path) -> None:
    root = project(tmp_path, "See Section~\\ref{missing}.")
    result = run(root, CheckConfig(type="pdf-inspection", target="main.pdf"))
    assert "Unresolved references or citations (??) in the PDF" in titles(result)


def test_an_unknown_venue_is_an_error(tmp_path: Path) -> None:
    result = run(tmp_path, CheckConfig(type="venue", venue="nowhere"))
    assert result.status == "error"


def test_packaging_replaces_identity_and_rescans(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "CITATION.cff").write_text(CFF, encoding="utf-8")
    (root / "README.md").write_text("By Jane Doe, github.com/janedoe/project\n", encoding="utf-8")
    (root / "veritas.yaml").write_text("secret local config\n", encoding="utf-8")
    (root / "shot.bin").write_bytes(b"\x00\xffJane Doe\x00")
    (root / "untracked.txt").write_text("Jane\n", encoding="utf-8")
    git(root, "add", "CITATION.cff", "README.md", "veritas.yaml", "shot.bin")
    git(root, "commit", "-qm", "x")
    result = package_anonymized(root, tmp_path / "supp.zip", identity_terms(root))
    names = zipfile.ZipFile(result.archive).namelist()
    assert "supplementary/README.md" in names
    assert "supplementary/veritas.yaml" not in names  # local configuration
    assert "supplementary/untracked.txt" not in names  # only tracked files
    readme = zipfile.ZipFile(result.archive).read("supplementary/README.md").decode()
    assert "Jane" not in readme and "janedoe" not in readme
    assert result.remaining == ["shot.bin"]  # binary still identifying: reported
