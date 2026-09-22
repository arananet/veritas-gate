"""Workspaces.

Repairs happen somewhere. That somewhere is explicit: the artifact in place, an
isolated copy, or a git worktree. Git is used when it is available and never
required — and Veritas never pushes, merges or rewrites the user's branches.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SKIP = {".git", ".veritas", ".venv", "venv", "node_modules", "__pycache__"}

WorkspaceMode = str  # "current" | "copy" | "worktree"


@dataclass(slots=True)
class Workspace:
    """Where a repair iteration does its work.

    ``root`` is the artifact: paths from ``artifact.paths`` resolve against it.
    ``top`` is the checkout that contains it, which differs from ``root`` when
    the artifact is a subdirectory of a repository — a git worktree brings the
    whole repository, so the artifact sits one or more levels down inside it.
    Changes and patches are tracked from ``top``, so work the agent does outside
    the artifact is still recorded rather than silently lost.
    """

    root: Path
    original_commit: str | None = None
    mode: WorkspaceMode = "current"
    source: Path | None = None
    top: Path | None = None
    _cleanup: list[Path] = field(default_factory=list, repr=False)
    _worktree_of: Path | None = field(default=None, repr=False)

    @property
    def scan_root(self) -> Path:
        return self.top or self.root

    @property
    def is_git(self) -> bool:
        return git_available() and _in_git_repo(self.scan_root)

    def snapshot(self) -> dict[str, float]:
        """Record file mtimes and sizes, so changes can be detected without git."""
        state: dict[str, float] = {}
        base = self.scan_root
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(base)
            # Relative parts only: a workspace under .veritas/workspaces/ would
            # otherwise skip every file it contains.
            if any(part in SKIP for part in relative.parts):
                continue
            stat = path.stat()
            state[relative.as_posix()] = stat.st_mtime + stat.st_size
        return state

    def changed_since(self, snapshot: dict[str, float]) -> list[str]:
        """Files added, removed or modified since ``snapshot``."""
        now = self.snapshot()
        changed = {name for name, value in now.items() if snapshot.get(name) != value}
        changed |= {name for name in snapshot if name not in now}
        return sorted(changed)

    def diff(self) -> str:
        """A unified patch of the working tree, or an empty string without git."""
        if not self.is_git:
            return ""
        result = _git(self.scan_root, "diff", "HEAD")
        if result is None:
            # No commit yet: show everything that is staged or untracked instead.
            _git(self.scan_root, "add", "-A", "-N")
            result = _git(self.scan_root, "diff")
        return result or ""

    def head_commit(self) -> str | None:
        if not self.is_git:
            return None
        return (_git(self.scan_root, "rev-parse", "HEAD") or "").strip() or None

    def cleanup(self) -> None:
        """Remove temporary state. A copy workspace is kept when it holds changes."""
        if self._worktree_of is not None:
            _git(self._worktree_of, "worktree", "prune")
        for path in self._cleanup:
            shutil.rmtree(path, ignore_errors=True)
        self._cleanup.clear()


def open_workspace(
    source: Path,
    mode: WorkspaceMode = "current",
    *,
    loop_id: str = "loop",
    base_dir: Path | None = None,
) -> Workspace:
    """Open a workspace over ``source`` in the requested mode.

    ``worktree`` degrades to ``copy`` when git is unavailable or the artifact is
    not in a repository, because a missing git must never fail an evaluation.
    """
    source = source.resolve()
    original = _head_commit(source)

    if mode == "current":
        return Workspace(root=source, original_commit=original, mode="current", source=source)

    target_base = base_dir or (source / ".veritas" / "workspaces")
    target_base.mkdir(parents=True, exist_ok=True)
    target = target_base / loop_id

    if mode == "worktree" and git_available() and _in_git_repo(source) and original:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        created = _git(source, "worktree", "add", "--detach", str(target), original)
        if created is not None:
            # A worktree checks out the whole repository. Point root back at the
            # artifact's own directory inside it, or every configured path
            # resolves to nothing.
            return Workspace(
                root=(target / _repo_relative(source)).resolve(),
                original_commit=original,
                mode="worktree",
                source=source,
                top=target.resolve(),
                _worktree_of=source,
            )

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*SKIP))
    return Workspace(root=target.resolve(), original_commit=original, mode="copy", source=source)


def temporary_workspace(source: Path) -> Workspace:
    """A throwaway copy, used by dry runs and tests."""
    target = Path(tempfile.mkdtemp(prefix="veritas-ws-"))
    destination = target / source.name
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*SKIP))
    return Workspace(
        root=destination,
        original_commit=_head_commit(source),
        mode="copy",
        source=source,
        _cleanup=[target],
    )


def git_available() -> bool:
    return shutil.which("git") is not None


def _in_git_repo(path: Path) -> bool:
    result = _git(path, "rev-parse", "--is-inside-work-tree")
    return (result or "").strip() == "true"


def _repo_relative(path: Path) -> Path:
    """Where ``path`` sits inside its repository, or "." at the top."""
    toplevel = (_git(path, "rev-parse", "--show-toplevel") or "").strip()
    if not toplevel:
        return Path()
    try:
        return path.resolve().relative_to(Path(toplevel).resolve())
    except ValueError:  # pragma: no cover - defensive
        return Path()


def _head_commit(path: Path) -> str | None:
    if not git_available():
        return None
    return (_git(path, "rev-parse", "HEAD") or "").strip() or None


def _git(cwd: Path, *args: str) -> str | None:
    """Run a git command, returning its stdout or None when it fails."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout
