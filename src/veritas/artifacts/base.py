"""Artifact abstraction.

An artifact is anything under evaluation: a manuscript, a repository, a design
document, an agent definition. The core never inspects artifact semantics; it
only collects addressable text segments that judges and checks can cite.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
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
    # Missed formats: a CITATION.cff listed in artifact.paths was skipped, and
    # one paper's reproduction scripts and test suite (.mjs) never reached a
    # judge, which then reported them as missing.
    ".cff",
    ".mjs",
    ".cjs",
    ".tsx",
    ".jsx",
    ".r",
    ".jl",
    ".tsv",
}

# Well-known files that carry no suffix at all.
TEXT_FILENAMES = {"LICENSE", "LICENCE", "NOTICE", "COPYING", "Makefile", "Dockerfile", "CITATION"}

DEFAULT_MAX_FILE_CHARS = 400_000

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
    # Per-file cap on what reaches a judge. Generous on purpose: truncating the
    # manuscript means judging a paper by its first three quarters, and current
    # models have the context for far more than this.
    max_file_chars: int = DEFAULT_MAX_FILE_CHARS
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
        collected.extend(_latex_dependencies(self.root, collected))
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

    def external_paths(self) -> list[str]:
        """Configured paths that resolve outside the artifact root.

        These work, but they change what a workspace must contain: a copy of
        the artifact directory alone will not include them.
        """
        outside: list[str] = []
        for rel in self.paths:
            target = (self.root / rel).resolve()
            if not target.exists():
                continue
            try:
                target.relative_to(self.root.resolve())
            except ValueError:
                outside.append(rel)
        return outside

    def truncated_files(self) -> list[str]:
        """Files the judges will only see part of."""
        return [
            segment.path for segment in self.segments() if len(segment.text) > self.max_file_chars
        ]

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


_LATEX_INCLUDE = re.compile(r"\\(?:input|include|subfile|bibliography)\s*\{([^}]+)\}")


def latex_includes(segment: ArtifactSegment) -> list[str]:
    """Paths, relative to the root, that a LaTeX segment pulls in."""
    if not segment.path.endswith(".tex"):
        return []
    folder = PurePosixPath(segment.path).parent
    found: list[str] = []
    for line in segment.text.splitlines():
        line = re.split(r"(?<!\\)%", line, maxsplit=1)[0]
        for group in _LATEX_INCLUDE.findall(line):
            for name in group.split(","):
                name = name.strip()
                if not name:
                    continue
                candidates = (
                    [name] if PurePosixPath(name).suffix else [f"{name}.tex", f"{name}.bib"]
                )
                # LaTeX resolves from the main file's directory, which a nested
                # file does not know; its own folder and each ancestor are tried.
                for base in (folder, *folder.parents):
                    found.extend(str(base / candidate) for candidate in candidates)
    return found


def _latex_dependencies(root: Path, segments: list[ArtifactSegment]) -> list[ArtifactSegment]:
    """Files a supplied LaTeX document inputs, read even when not listed.

    A manuscript is its main file plus everything it inputs. Listing the main
    file alone once sent eight judges a paper whose results table -- a
    generated \\input -- was absent, and five of them reported it missing.
    Only files inside the artifact root are followed.
    """
    have = {segment.path for segment in segments}
    pending = list(segments)
    added: list[ArtifactSegment] = []
    resolved_root = root.resolve()
    while pending:
        segment = pending.pop()
        for rel in latex_includes(segment):
            if rel in have:
                continue
            target = (root / rel).resolve()
            if not target.is_file() or not _within(target, resolved_root):
                continue
            loaded = _read_file(root, target)
            if loaded is None:
                continue
            have.add(loaded.path)
            added.append(loaded)
            pending.append(loaded)
    return added


def _read_file(root: Path, target: Path) -> ArtifactSegment | None:
    if target.suffix.lower() not in TEXT_SUFFIXES and target.name not in TEXT_FILENAMES:
        return None
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return ArtifactSegment(path=relative_path(root, target), text=text)


def relative_path(root: Path, target: Path) -> str:
    """Describe ``target`` relative to ``root``, even when it sits outside it.

    An absolute path here would put the user's home directory into every judge
    prompt, and would travel into findings and repair plans — where a path
    outside the workspace is one an agent could write to. A ``../`` relative
    path stays portable across the workspace copies the repair loop makes.
    """
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        pass
    try:
        return PurePosixPath(os.path.relpath(target, root)).as_posix()
    except ValueError:  # pragma: no cover - different drive on Windows
        return target.as_posix()


def _read_tree(root: Path, directory: Path) -> list[ArtifactSegment]:
    """Walk a directory, following symlinked directories inside the artifact.

    `Path.rglob` does not descend into a symlinked directory, and did so
    silently: evidence cited through a stable `current-run` link — the ordinary
    way to cite something that is regenerated under a new name each time — was
    invisible to every judge, while the same files under their real name were
    read normally. The walk is explicit now, so the two rules that bound it are
    visible where they are enforced.
    """
    segments: list[ArtifactSegment] = []
    walked: set[Path] = set()
    try:
        boundary = directory.resolve()
    except OSError:  # pragma: no cover - a broken link or a vanished path
        return []

    def walk(current: Path, ancestors: frozenset[Path]) -> None:
        try:
            real = current.resolve()
        except OSError:  # pragma: no cover - a broken link or a vanished path
            return
        # The boundary is the directory the operator configured, not the
        # artifact root: a configured path may legitimately sit outside the root
        # (and is reported by external_paths), but a symlink found inside it may
        # not wander further, or a link to a home directory would put files
        # nobody offered into a judge's prompt.
        if real != boundary and not _within(real, boundary):
            return
        # A cycle is a directory reappearing beneath itself. Reaching one real
        # directory by two different spellings is not a cycle: documents cite
        # both the stable link and the real name, and both must be readable.
        if real in ancestors or current in walked:
            return
        walked.add(current)
        try:
            entries = sorted(current.iterdir())
        except OSError:  # pragma: no cover - unreadable directory
            return
        for entry in entries:
            if _is_skipped(root, entry):
                continue
            try:
                is_dir = entry.is_dir()
                is_file = entry.is_file()
            except OSError:  # a broken symlink
                continue
            if is_dir:
                walk(entry, ancestors | {real})
            elif is_file:
                segment = _read_file(root, entry)
                if segment is not None:
                    segments.append(segment)

    walk(directory, frozenset())
    return sorted(segments, key=lambda item: item.path)


def _within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True


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
