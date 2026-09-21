"""Artifact abstraction.

An artifact is anything under evaluation: a manuscript, a repository, a design
document, an agent definition. The core never inspects artifact semantics; it
only collects addressable text segments that judges and checks can cite.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TEXT_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".tex",
    ".bib",
    ".rst",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".cfg",
    ".ini",
    ".csv",
    ".sql",
    ".sh",
    ".ts",
    ".js",
    ".go",
    ".rs",
    ".java",
    ".rb",
}

SKIP_DIRECTORIES = {
    ".git",
    ".veritas",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".tox",
}


@dataclass(slots=True)
class ArtifactSegment:
    """A citable chunk of artifact content."""

    path: str
    text: str
    kind: str = "text"

    def truncated(self, limit: int) -> ArtifactSegment:
        if len(self.text) <= limit:
            return self
        return ArtifactSegment(
            path=self.path,
            text=self.text[:limit] + f"\n... [truncated at {limit} characters]",
            kind=self.kind,
        )


@dataclass(slots=True)
class Artifact:
    """Something being evaluated."""

    id: str
    type: str
    root: Path
    paths: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    _segments: list[ArtifactSegment] | None = field(default=None, repr=False)

    def segments(self) -> list[ArtifactSegment]:
        """Return the artifact's readable text segments, loading them once."""
        if self._segments is None:
            self._segments = list(self._load_segments())
        return self._segments

    def _load_segments(self) -> list[ArtifactSegment]:
        collected: list[ArtifactSegment] = []
        for rel in self.paths:
            target = (self.root / rel).resolve()
            if target.is_dir():
                collected.extend(_read_tree(self.root, target))
            elif target.is_file():
                segment = _read_file(self.root, target)
                if segment is not None:
                    collected.append(segment)
        seen: set[str] = set()
        unique: list[ArtifactSegment] = []
        for segment in collected:
            if segment.path in seen:
                continue
            seen.add(segment.path)
            unique.append(segment)
        unique.sort(key=lambda item: item.path)
        return unique

    def file_list(self) -> list[str]:
        return [segment.path for segment in self.segments()]

    def commit_sha(self) -> str | None:
        """Return the artifact's git commit SHA when it lives in a repository."""
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None


def _read_file(root: Path, target: Path) -> ArtifactSegment | None:
    if target.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        rel = target.as_posix()
    return ArtifactSegment(path=rel, text=text)


def _read_tree(root: Path, directory: Path) -> list[ArtifactSegment]:
    segments: list[ArtifactSegment] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if _is_skipped(root, path):
            continue
        segment = _read_file(root, path)
        if segment is not None:
            segments.append(segment)
    return segments


def _is_skipped(root: Path, path: Path) -> bool:
    """Match the skip list against the path *relative to the artifact root*.

    Matching absolute parts would hide the whole artifact whenever the root
    itself sits under a skipped name — which is exactly what happens to a
    repair workspace living under ``.veritas/workspaces/``.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return any(part in SKIP_DIRECTORIES for part in relative.parts)
