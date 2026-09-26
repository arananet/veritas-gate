"""Reading an artifact from disk."""

from __future__ import annotations

from pathlib import Path

from veritas.artifacts import Artifact

# ------------------------------------------- symlinked directories
#
# Evidence that is regenerated should be cited by a name that survives
# regeneration, and a symlink to the current run is the ordinary way to do it.
# Veritas could not read through one, so every file cited by the stable name was
# invisible to the judges while the same files, under their real suffixed name,
# were read normally.


def test_a_file_under_a_symlinked_directory_is_read(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    (evidence / "run-abc").mkdir(parents=True)
    (evidence / "run-abc" / "report.txt").write_text("18 passed\n", encoding="utf-8")
    (evidence / "current").symlink_to("run-abc")

    artifact = Artifact(id="a", type="paper", root=tmp_path, paths=["evidence"])
    files = artifact.file_list()

    assert "evidence/current/report.txt" in files
    assert "evidence/run-abc/report.txt" in files
    texts = {item.path: item.text for item in artifact.segments()}
    assert texts["evidence/current/report.txt"] == texts["evidence/run-abc/report.txt"]


def test_a_symlink_may_not_wander_outside_the_configured_directory(tmp_path: Path) -> None:
    """A link to a home directory would put files nobody offered into a prompt."""
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secrets.txt").write_text("not yours\n", encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "escape").symlink_to(outside)

    artifact = Artifact(id="a", type="paper", root=tmp_path, paths=["evidence"])
    assert artifact.file_list() == []
    assert "not yours" not in " ".join(item.text for item in artifact.segments())


def test_a_symlink_cycle_terminates(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    (evidence / "inner").mkdir(parents=True)
    (evidence / "inner" / "note.txt").write_text("x\n", encoding="utf-8")
    (evidence / "inner" / "loop").symlink_to(evidence)

    artifact = Artifact(id="a", type="paper", root=tmp_path, paths=["evidence"])
    assert "evidence/inner/note.txt" in artifact.file_list()


def test_a_symlink_to_a_file_is_read(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "real.txt").write_text("content\n", encoding="utf-8")
    (evidence / "alias.txt").symlink_to("real.txt")

    artifact = Artifact(id="a", type="paper", root=tmp_path, paths=["evidence"])
    assert "evidence/alias.txt" in artifact.file_list()


def test_a_broken_symlink_is_skipped_without_raising(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "real.txt").write_text("content\n", encoding="utf-8")
    (evidence / "gone").symlink_to("nowhere-at-all")

    artifact = Artifact(id="a", type="paper", root=tmp_path, paths=["evidence"])
    assert artifact.file_list() == ["evidence/real.txt"]


def test_formats_research_repositories_use_are_read(tmp_path: Path) -> None:
    """A CITATION.cff and a paper's .mjs test suite were silently skipped."""
    names = [
        "CITATION.cff",
        "paper.mjs",
        "a.cjs",
        "b.tsx",
        "c.jsx",
        "d.r",
        "e.jl",
        "LICENSE",
        "Makefile",
    ]
    for name in names:
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    artifact = Artifact(id="a", type="paper", root=tmp_path, paths=["."])
    listed = set(artifact.file_list())
    assert set(names) <= listed
    assert "image.png" not in listed


def test_latex_inputs_follow_the_main_file(tmp_path: Path) -> None:
    """A manuscript is its main file plus what it inputs."""
    paper = tmp_path / "paper"
    paper.mkdir()
    (paper / "main.tex").write_text(
        "\\input{table.tex}\n\\input{sections/intro}\n% \\input{commented}\n\\bibliography{refs}\n",
        encoding="utf-8",
    )
    (paper / "table.tex").write_text("a & b\n", encoding="utf-8")
    (paper / "sections").mkdir()
    (paper / "sections" / "intro.tex").write_text("\\input{deep}\n", encoding="utf-8")
    (paper / "deep.tex").write_text("deep\n", encoding="utf-8")
    (paper / "commented.tex").write_text("no\n", encoding="utf-8")
    (paper / "refs.bib").write_text("@misc{a, title={t}}\n", encoding="utf-8")
    artifact = Artifact(id="a", type="paper", root=tmp_path, paths=["paper/main.tex"])
    files = set(artifact.file_list())
    assert {
        "paper/main.tex",
        "paper/table.tex",
        "paper/sections/intro.tex",
        "paper/refs.bib",
    } <= files
    # Relative to the file that inputs it, as LaTeX resolves from the main file's
    # directory only when run there: intro's \input{deep} resolves beside intro.
    assert "paper/commented.tex" not in files
