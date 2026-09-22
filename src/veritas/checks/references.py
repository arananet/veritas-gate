"""Reference integrity: does what a document cites actually exist?

A manuscript cited an evidence directory that was not there. Eight LLM judges
found it, over three evaluations, at roughly three quarters of a million tokens
each — to settle a question a path lookup answers for nothing. Judges are for
what needs judgement.

The check separates two outcomes a judge reports identically and which are not
the same problem:

* the target exists nowhere — the document is wrong;
* the target exists, but ``artifact.paths`` never supplied it — the document is
  right and the configuration is wrong.

Conflated, the second sends an author rewriting correct prose to match a truth
Veritas could not see. That cost a day to tell apart by hand.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Literal

from veritas.artifacts.base import Artifact
from veritas.checks.base import check_finding
from veritas.config import CheckConfig
from veritas.models.evaluation import CheckResult
from veritas.models.finding import Finding

# Markdown links, and paths inside inline code. Both carry citations in the
# papers this was built for.
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?\s*\)")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

# A path-shaped thing in inline code must contain a separator. A suffix alone
# is not enough: `0.21.0`, `Tracer.record` and `textcomp.sty` all end in one and
# none is a file in the repository. Prose is full of dotted tokens.
_PATH_LIKE = re.compile(r"^[\w.][\w./@+-]*/[\w./@+-]*$")

_EXTERNAL = ("http://", "https://", "mailto:", "ftp://", "//")


class ReferenceIntegrityCheck:
    """Resolve every relative path the configured documents cite."""

    def __init__(self, name: str, config: CheckConfig) -> None:
        self.name = name
        self.config = config

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        documents = self._documents(artifact)
        if not documents:
            return CheckResult(
                check=self.name,
                status="skipped",
                summary="no documents configured to check",
                duration_seconds=_since(started),
            )

        unreadable = [path for path, text in documents if not text.strip()]
        if len(unreadable) == len(documents):
            return CheckResult(
                check=self.name,
                status="error",
                summary=f"no readable text in {', '.join(unreadable)}",
                duration_seconds=_since(started),
            )

        supplied = _supplied_paths(artifact)
        findings: list[Finding] = []
        # Every citation to a path that exists but was not supplied has the same
        # fix -- one line in artifact.paths -- so they are grouped by that line.
        # Reported one per citation, thirty findings described four edits.
        unsupplied: dict[str, list[str]] = {}
        checked = 0

        for path, text in documents:
            for line_no, target, from_code in _citations(text):
                if from_code and not _is_repository_path(artifact.root, path, target):
                    continue  # a slashed token in prose, not a local path
                resolved = _resolve(artifact.root, path, target)
                if resolved is None:
                    continue  # outside the repository; not this check's business
                checked += 1
                if not resolved.exists():
                    findings.append(self._dangling(len(findings) + 1, path, line_no, target))
                elif not _is_supplied(artifact.root, resolved, supplied):
                    rel = _relative(artifact.root, resolved)
                    unsupplied.setdefault(rel, []).append(f"{path}:{line_no} -> {target}")

        for rel, citations in sorted(unsupplied.items()):
            findings.append(self._unsupplied(len(findings) + 1, rel, citations))

        status: Literal["pass", "fail"] = "pass" if not findings else "fail"
        summary = (
            f"{checked} reference(s) checked, all resolve"
            if not findings
            else f"{len(findings)} problem(s) across {checked} reference(s)"
        )
        return CheckResult(
            check=self.name,
            status=status,
            summary=summary,
            findings=findings,
            duration_seconds=_since(started),
        )

    def _documents(self, artifact: Artifact) -> list[tuple[str, str]]:
        wanted = self.config.required_paths or ([self.config.target] if self.config.target else [])
        segments = artifact.segments()
        if not wanted:
            return []
        return [(item.path, item.text) for item in segments if item.path in set(wanted)]

    def _dangling(self, index: int, document: str, line: int, target: str) -> Finding:
        return check_finding(
            self.name,
            index,
            title=f"Cited path does not exist: {target}",
            severity=self.config.severity_on_failure,
            description=(
                f"{document} line {line} cites `{target}`, which exists nowhere in the "
                "repository. Either the reference is stale — a regenerated directory "
                "whose name changed, a renamed file — or the target was never created."
            ),
            location=f"{document}:{line}",
            evidence=[f"{document}:{line} -> {target}"],
            recommendation=(
                "Point the citation at a path that exists. Where the target is "
                "regenerated and its name changes each time, cite a stable name that "
                "does not, so the next regeneration cannot break the reference again."
            ),
        )

    def _unsupplied(self, index: int, rel: str, citations: list[str]) -> Finding:
        count = len(citations)
        where = "1 citation" if count == 1 else f"{count} citations"
        return check_finding(
            # Its own category, so a deliberate exclusion can be accepted as a
            # known risk without also silencing a genuine dangling reference.
            f"{self.name}/unsupplied",
            index,
            title=f"Cited path exists but was not supplied to Veritas: {rel}",
            severity=self.config.severity_on_unsupplied,
            description=(
                f"{rel} exists on disk and is cited by {where}, but it is not within "
                "the artifact's configured paths. Judges cannot see it and may report "
                "it as missing evidence. Either add it to artifact.paths, or, if it is "
                "excluded on purpose — a lockfile, a PDF, prior versions — leave it out "
                "and accept this finding."
            ),
            location=citations[0].split(" -> ")[0],
            evidence=citations[:10],
            recommendation=f"Add `{rel}` to artifact.paths in veritas.yaml.",
        )


def _citations(text: str) -> list[tuple[int, str, bool]]:
    """Every relative path the text cites: line, target, and whether it is inline code.

    A Markdown link to a relative path is unambiguous. A slashed token in
    inline code is not, so the caller checks it harder.
    """
    found: list[tuple[int, str, bool]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in _MARKDOWN_LINK.finditer(line):
            target = match.group(1).strip()
            if _is_local(target):
                found.append((line_no, target, False))
        for match in _INLINE_CODE.finditer(line):
            target = match.group(1).strip()
            if _is_local(target) and _PATH_LIKE.match(target):
                found.append((line_no, target, True))
    return found


def _is_repository_path(root: Path, document: str, target: str) -> bool:
    """Does this token's first segment name something the repository has?

    Inline code carries plenty of slashed things that are not local paths --
    `google/A2UI` is an upstream owner and repository. Requiring the first
    segment to exist keeps the check on paths without a vendor list.
    """
    clean = target.split("#", 1)[0].lstrip("./").strip("/")
    head = clean.split("/", 1)[0]
    if not head:
        return False
    return (root / head).exists() or ((root / document).parent / head).exists()


def _is_local(target: str) -> bool:
    if not target or target.startswith("#"):
        return False  # a fragment within this document
    return not target.lower().startswith(_EXTERNAL)


def _resolve(root: Path, document: str, target: str) -> Path | None:
    """Resolve a citation, relative to the citing document or to the root.

    Both spellings are ordinary in the same document: a Markdown link is
    written relative to the file it sits in, while a path quoted in prose is
    usually written from the repository root. Trying only the first reports
    `paper/evidence/run` as missing from `paper/manuscript.md`, because it
    resolves to `paper/paper/evidence/run`.
    """
    clean = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not clean:
        return None

    candidates: list[Path] = []
    if clean.startswith("/"):
        candidates.append(root / clean.lstrip("/"))
    else:
        candidates.append((root / document).parent / clean)
        candidates.append(root / clean)

    inside: Path | None = None
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root.resolve())
        except (ValueError, OSError):
            continue  # outside the repository; not this check's business
        if resolved.exists():
            return resolved
        inside = inside or resolved
    # Nothing resolved to something real; report against the first spelling
    # that at least stayed inside the repository.
    return inside


def _supplied_paths(artifact: Artifact) -> list[Path]:
    root = artifact.root.resolve()
    supplied: list[Path] = []
    for rel in artifact.paths:
        try:
            supplied.append((artifact.root / rel).resolve())
        except OSError:  # pragma: no cover - defensive
            continue
    return supplied or [root]


def _is_supplied(root: Path, target: Path, supplied: list[Path]) -> bool:
    for path in supplied:
        if target == path:
            return True
        try:
            target.relative_to(path)
        except ValueError:
            continue
        return True
    return False


def _relative(root: Path, target: Path) -> str:
    try:
        return str(target.relative_to(root.resolve()))
    except ValueError:  # pragma: no cover - defensive
        return str(target)


def _since(started: float) -> float:
    return round(time.monotonic() - started, 3)
