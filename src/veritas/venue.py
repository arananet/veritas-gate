"""Venue rules, identity terms and anonymized packaging.

Preparing a paper for a double-blind venue meant, by hand: listing every string
that identifies the author (name, handle, email, ORCID, archive DOIs), finding
each in the PDF text, its metadata and the source, moving the appendix after
the references, counting main-text pages, and producing a supplementary zip
with the identity replaced and the tests still passing. Everything here is that
list, made repeatable.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_DOI = re.compile(r"10\.\d{4,9}/[^\s\"'<>{}),;\]]+")


@dataclass(slots=True)
class VenueRules:
    name: str
    anonymous: bool
    template_package: str | None = None
    main_page_limit: int | None = None
    appendix_after_references: bool = False
    broader_impact: bool = False


VENUES: dict[str, VenueRules] = {
    "tmlr": VenueRules(
        "tmlr",
        anonymous=True,
        template_package="tmlr",
        main_page_limit=12,
        appendix_after_references=True,
        broader_impact=True,
    ),
    "arxiv": VenueRules("arxiv", anonymous=False),
    "preprint": VenueRules("preprint", anonymous=False),
}


def rules_for(name: str) -> VenueRules:
    try:
        return VENUES[name.lower()]
    except KeyError as exc:
        known = ", ".join(sorted(VENUES))
        raise ValueError(f"unknown venue '{name}'; known venues: {known}") from exc


def identity_terms(root: Path, extra: list[str] | None = None) -> list[str]:
    """Strings that identify the author, from CITATION.cff and the git remote.

    Names, ORCID, email and the repository owner come from the project itself,
    so nothing has to be typed twice; ``extra`` adds anything else (an
    employer, a co-author's handle).
    """
    terms: set[str] = set(t for t in (extra or []) if t.strip())
    cff = root / "CITATION.cff"
    if cff.is_file():
        text = cff.read_text(encoding="utf-8", errors="replace")
        family = re.findall(r"family-names:\s*['\"]?([^'\"\n]+)", text)
        given = re.findall(r"given-names:\s*['\"]?([^'\"\n]+)", text)
        for surname, first in zip(family, given, strict=False):
            terms.update({surname.strip(), first.strip(), f"{first.strip()} {surname.strip()}"})
        terms.update(
            m.strip()
            for m in re.findall(r"orcid:\s*['\"]?(?:https?://orcid\.org/)?([\d-]{19})", text)
        )
        terms.update(m.strip() for m in re.findall(r"email:\s*['\"]?([^'\"\s]+)", text))
        terms.update(d.rstrip(".") for d in _DOI.findall(text) if "zenodo" in d)
        owner = re.search(r"github\.com/([\w.-]+)/", text)
        if owner:
            terms.add(owner.group(1))
    remote = _git(root, "remote", "get-url", "origin")
    owner = re.search(r"github\.com[/:]([\w.-]+)/", remote or "")
    if owner:
        terms.add(owner.group(1))
    return sorted((t for t in terms if len(t) >= 3), key=len, reverse=True)


def find_terms(text: str, terms: list[str]) -> list[str]:
    found = []
    for term in terms:
        if re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text, re.IGNORECASE):
            found.append(term)
    return found


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def pdf_text(pdf: Path) -> str | None:
    try:
        return subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def pdf_info(pdf: Path) -> dict[str, str]:
    try:
        output = subprocess.run(
            ["pdfinfo", str(pdf)], capture_output=True, text=True, timeout=60, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    info: dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition(":")
        info[key.strip()] = value.strip()
    return info


@dataclass(slots=True)
class PackageResult:
    archive: Path
    files: int
    replaced: dict[str, int] = field(default_factory=dict)
    remaining: list[str] = field(default_factory=list)


def package_anonymized(
    root: Path,
    output: Path,
    terms: list[str],
    exclude: list[str] | None = None,
    replacement: str = "anonymous",
) -> PackageResult:
    """Copy the tracked files, replace identity terms in text, zip, and re-scan.

    Only git-tracked files are copied, so history, ignored files and local
    configuration never enter the archive. Binary files are copied unchanged
    and reported if they still contain a term.
    """
    listed = _git(root, "ls-files")
    if listed is None:
        raise ValueError("packaging needs a git repository: only tracked files are included")
    skip = set(exclude or []) | {"veritas.yaml", "veritas.lock.json"}
    staging = output.with_suffix("")
    if staging.exists():
        shutil.rmtree(staging)
    name = "supplementary"
    base = staging / name
    replaced: dict[str, int] = {}
    remaining: list[str] = []
    count = 0
    patterns = [
        (re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", re.IGNORECASE), term) for term in terms
    ]
    for rel in listed.splitlines():
        if not rel or rel in skip or rel.startswith(".veritas/"):
            continue
        source = root / rel
        if not source.is_file():
            continue
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        count += 1
        try:
            text = source.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            shutil.copy2(source, target)
            data = source.read_bytes()
            if any(term.encode() in data for term in terms):
                remaining.append(rel)
            continue
        for pattern, term in patterns:
            text, n = pattern.subn(_placeholder(term, replacement), text)
            if n:
                replaced[term] = replaced.get(term, 0) + n
        target.write_text(text, encoding="utf-8")
    for path in base.rglob("*"):
        if path.is_file():
            try:
                body = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if find_terms(body, terms):
                remaining.append(path.relative_to(base).as_posix())
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(base.rglob("*")):
            if path.is_file():
                archive.write(path, (Path(name) / path.relative_to(base)).as_posix())
    shutil.rmtree(staging)
    return PackageResult(output, count, replaced, sorted(set(remaining)))


def _placeholder(term: str, replacement: str) -> str:
    if _DOI.fullmatch(term):
        return "[DOI withheld for review]"
    if re.fullmatch(r"[\d-]{19}", term):
        return "[ORCID withheld]"
    if "@" in term:
        return "anonymous@example.org"
    return replacement
