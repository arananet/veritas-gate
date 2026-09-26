"""Frozen-evidence integrity and release consistency, from git and CITATION.cff.

A frozen validation report was edited in place by two later commits, and a
release archived the edited version; nobody noticed until the files were
compared by hand. Frozen evidence is append-only: files may be added, never
changed. This check asks git whether any frozen file was modified after it was
first committed, or is modified now, and whether the release metadata agrees
with itself.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Literal

from veritas.artifacts.base import Artifact
from veritas.checks.base import check_finding
from veritas.config import CheckConfig
from veritas.derive import frozen_files
from veritas.models.evaluation import CheckResult
from veritas.models.finding import Finding

_DOI = re.compile(r"10\.\d{4,9}/[^\s\"'<>{}),;\]]+")


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


class FrozenIntegrityCheck:
    def __init__(self, name: str, config: CheckConfig, frozen: list[str]) -> None:
        self.name = name
        self.config = config
        self.frozen = frozen
        self.findings: list[Finding] = []

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        self.findings = []
        root = artifact.root
        top = (_git(root, "rev-parse", "--show-toplevel") or "").strip()
        if not top:
            return self._result(started, "skipped", "not a git repository")
        top_path = Path(top)
        notes: list[str] = []
        if self.frozen:
            self._frozen(root, top_path, notes)
        self._release(root, top_path)
        if (_git(top_path, "rev-parse", "--is-shallow-repository") or "").strip() == "true":
            notes.append("shallow clone: history before the clone depth was not checked")
        if self.findings:
            return self._result(
                started, "fail", f"{len(self.findings)} integrity problem(s)", notes
            )
        summary = "frozen evidence unmodified; release metadata consistent"
        return self._result(started, "pass", summary, notes)

    def _frozen(self, root: Path, top: Path, notes: list[str]) -> None:
        files = frozen_files(root, self.frozen)
        if not files:
            notes.append("no file matches the frozen patterns")
            return
        prefix = root.resolve().relative_to(top.resolve()).as_posix()
        paths = [f"{prefix}/{f}" if prefix != "." else f for f in files]

        dirty = (_git(top, "status", "--porcelain", "--", *paths) or "").splitlines()
        changed_now = [line[3:] for line in dirty if line[:2].strip() and not line.startswith("??")]
        if changed_now:
            self._add(
                f"{len(changed_now)} frozen file(s) modified in the working tree",
                self.config.severity_on_failure,
                "Frozen evidence records what an experiment produced; these files differ "
                "from their committed version.",
                changed_now[0],
                "Restore them (`git checkout -- <path>`) and record any correction "
                "append-only, in a new file.",
                changed_now[:10],
            )

        modified: list[str] = []
        for path in paths:
            log = _git(
                top, "log", "--diff-filter=M", "--format=%h %ad %s", "--date=short", "--", path
            )
            for line in (log or "").splitlines():
                modified.append(f"{path}: {line}")
        if modified:
            self._add(
                "Frozen evidence was modified after it was committed",
                self.config.severity_on_failure,
                "Frozen evidence is append-only. These commits changed a frozen file in "
                "place, so any release made after them archives a record that is not what "
                "the experiment produced.",
                modified[0].split(":", 1)[0],
                "Restore the original content from the commit that froze it, and move "
                "each correction to a new file (for example corrections/) that the paper "
                "cites as an erratum.",
                modified[:20],
            )

    def _release(self, root: Path, top: Path) -> None:
        cff = root / "CITATION.cff"
        if not cff.is_file():
            return
        text = cff.read_text(encoding="utf-8", errors="replace")
        version = re.search(r"^version:\s*['\"]?([^'\"\n]+)", text, re.M)
        if version:
            value = version.group(1).strip()
            tags = set((_git(top, "tag", "--list") or "").split())
            if value not in tags and f"v{value}" not in tags:
                self._add(
                    f"CITATION.cff version {value} has no matching git tag",
                    "minor",
                    "A citation names a version that no tag identifies, so a reader cannot "
                    "check out what was cited.",
                    "CITATION.cff",
                    f"Tag the release as v{value}, or correct `version:`.",
                )
        cited = set(_DOI.findall(text))
        target = self.config.target
        if target and (root / target).is_file() and cited:
            manuscript = (root / target).read_text(encoding="utf-8", errors="replace")
            in_paper = {d.rstrip(".") for d in _DOI.findall(manuscript) if "zenodo" in d}
            unknown = sorted(in_paper - cited)
            if unknown:
                self._add(
                    "The manuscript cites an archive DOI that CITATION.cff does not list",
                    "minor",
                    "The paper and the citation metadata disagree about which archive "
                    "holds the work.",
                    target,
                    "Add the DOI to CITATION.cff identifiers, or correct the manuscript.",
                    unknown,
                )

    def _add(
        self,
        title: str,
        severity: str,
        description: str,
        location: str,
        recommendation: str,
        evidence: list[str] | None = None,
    ) -> None:
        self.findings.append(
            check_finding(
                self.name,
                len(self.findings) + 1,
                title=title,
                severity=severity,  # type: ignore[arg-type]
                description=description,
                location=location,
                evidence=evidence or [],
                recommendation=recommendation,
            )
        )

    def _result(
        self,
        started: float,
        status: Literal["pass", "fail", "skipped"],
        summary: str,
        notes: list[str] | None = None,
    ) -> CheckResult:
        if notes:
            summary = f"{summary} ({'; '.join(notes)})"
        return CheckResult(
            check=self.name,
            status=status,
            summary=summary,
            findings=self.findings,
            duration_seconds=round(time.monotonic() - started, 3),
        )
