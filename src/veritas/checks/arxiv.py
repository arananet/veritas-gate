"""arXiv readiness: will the LaTeX source survive submission as it stands?

arXiv publishes the source it is given and builds it itself. Four things go
wrong there that no judge needs to read the paper to find:

* a file the source pulls in (``\\input``, ``\\includegraphics``, the
  bibliography) is not in the package, so the build fails;
* arXiv does not run BibTeX, so a paper using ``\\bibliography`` without its
  ``.bbl`` builds with every citation as ``[?]``;
* a ``\\cite`` key has no entry, or an entry lacks what a reference needs;
* a comment meant for co-authors ("% TODO: reviewer 2 is right") is published.

Nothing is compiled and nothing is fetched: the check reads files, so it runs
offline and executes nothing from the repository. Compiling the source is the
operator's to opt into, through an allow-listed command check.
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

_GRAPHIC_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".ps")
_INPUT = re.compile(r"\\(input|include|subfile)\s*\{([^}]+)\}")
_GRAPHIC = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_GRAPHICSPATH = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^}]*\})+)\s*\}")
_BIBLIOGRAPHY = re.compile(r"\\bibliography\s*\{([^}]+)\}")
_ADDBIB = re.compile(r"\\addbibresource\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_CITE = re.compile(r"\\(?:no)?cite[a-zA-Z]*\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]+)\}")
_BIB_ENTRY = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.IGNORECASE)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
# Words that mark a comment as a note to self or to co-authors.
_PRIVATE = re.compile(
    r"\b(todo|fixme|xxx|hack|reviewer|rebuttal|do not submit|remove before|internal)\b",
    re.IGNORECASE,
)
_REQUIRED_FIELDS = {
    "article": ("author", "title", "year"),
    "inproceedings": ("author", "title", "year"),
    "book": ("title", "year"),
    "misc": ("title",),
}
_SKIP_SIZE = {".git", ".veritas", "__pycache__", "node_modules", ".venv"}


class ArxivPackageCheck:
    """Check a LaTeX paper's source the way arXiv will receive it."""

    def __init__(self, name: str, config: CheckConfig) -> None:
        self.name = name
        self.config = config
        self.findings: list[Finding] = []

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        self.findings = []
        main, why = self._main(artifact)
        if main is None:
            return CheckResult(
                check=self.name,
                status="skipped",
                summary=why,
                duration_seconds=round(time.monotonic() - started, 3),
            )

        root = artifact.root.resolve()
        package = main.parent
        sources = self._sources(root, main)
        text = "\n".join(
            _strip_comments(source.read_text("utf-8", "replace")) for source in sources
        )

        self._graphics(root, package, main, text)
        bibs = self._bibliography(root, package, main, text)
        self._citations(root, text, bibs)
        self._comments(root, sources)
        self._size(root, package)

        status: Literal["pass", "fail"] = "fail" if self.findings else "pass"
        rel = _rel(root, main)
        summary = (
            f"{rel}: {len(sources)} source file(s) ready for arXiv"
            if not self.findings
            else f"{rel}: {len(self.findings)} problem(s) before submitting to arXiv"
        )
        return CheckResult(
            check=self.name,
            status=status,
            summary=summary,
            findings=self.findings,
            duration_seconds=round(time.monotonic() - started, 3),
        )

    # ------------------------------------------------------------ discovery

    def _main(self, artifact: Artifact) -> tuple[Path | None, str]:
        if self.config.target:
            target = (artifact.root / self.config.target).resolve()
            if not target.is_file():
                return None, f"target {self.config.target} does not exist"
            return target, ""
        candidates = [
            segment.path
            for segment in artifact.segments()
            if segment.path.endswith(".tex") and "\\documentclass" in segment.text
        ]
        if not candidates:
            return None, "no LaTeX main file (\\documentclass) among the supplied files"
        if len(candidates) > 1:
            return None, (
                f"several LaTeX main files ({', '.join(candidates[:4])}); "
                "set target to the one you submit"
            )
        return (artifact.root / candidates[0]).resolve(), ""

    def _sources(self, root: Path, main: Path) -> list[Path]:
        """The main file and everything it inputs, recursively."""
        seen: list[Path] = []
        pending = [main]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.append(current)
            body = _strip_comments(current.read_text("utf-8", "replace"))
            for match in _INPUT.finditer(body):
                name = match.group(2).strip()
                found = _find(main.parent, name, (".tex",))
                if found is None:
                    self._missing(root, current, body, match.start(), name, "input")
                else:
                    pending.append(found)
        return seen

    # --------------------------------------------------------------- checks

    def _graphics(self, root: Path, package: Path, main: Path, text: str) -> None:
        folders = [package]
        for group in _GRAPHICSPATH.findall(text):
            folders.extend(package / item for item in re.findall(r"\{([^}]*)\}", group))
        for match in _GRAPHIC.finditer(text):
            name = match.group(1).strip()
            if not any(_find(folder, name, _GRAPHIC_SUFFIXES) for folder in folders):
                self._missing(root, main, None, None, name, "figure")

    def _bibliography(self, root: Path, package: Path, main: Path, text: str) -> list[Path]:
        bibs: list[Path] = []
        names = [n for m in _BIBLIOGRAPHY.findall(text) for n in m.split(",")]
        names += _ADDBIB.findall(text)
        for name in (n.strip() for n in names):
            found = _find(package, name, (".bib",))
            if found is None:
                self._missing(root, main, None, None, name, "bibliography")
            else:
                bibs.append(found)
        uses_bibtex = bool(_BIBLIOGRAPHY.search(text))
        bbl = main.with_suffix(".bbl")
        if uses_bibtex and not bbl.is_file():
            self._add(
                title=f"No {bbl.name} next to {main.name}: arXiv will not build the bibliography",
                severity="minor",
                description=(
                    "arXiv does not run BibTeX. A submission that uses \\bibliography "
                    "without the generated .bbl file builds with every citation shown "
                    "as [?]."
                ),
                location=_rel(root, main),
                recommendation=(
                    f"Build the paper locally and include {bbl.name} in the upload, "
                    "named after the main .tex file."
                ),
            )
        return bibs

    def _citations(self, root: Path, text: str, bibs: list[Path]) -> None:
        if not bibs:
            return
        entries: dict[str, tuple[str, str, Path]] = {}
        for bib in bibs:
            for entry_type, key, body in _bib_entries(bib.read_text("utf-8", "replace")):
                entries[key] = (entry_type, body, bib)

        cited = {
            key.strip()
            for group in _CITE.findall(text)
            for key in group.split(",")
            if key.strip() and key.strip() != "*"
        }
        missing = sorted(cited - entries.keys())
        if missing:
            self._add(
                title=f"{len(missing)} citation key(s) have no bibliography entry",
                severity=self.config.severity_on_failure,
                description=(
                    "These keys are cited in the source but defined in no supplied "
                    ".bib file; each renders as [?]."
                ),
                evidence=missing[:20],
                recommendation="Add the entries, or correct the keys.",
            )

        incomplete: list[str] = []
        bad_doi: list[str] = []
        for key in sorted(cited & entries.keys()):
            entry_type, body, bib = entries[key]
            fields = _fields(body)
            required = _REQUIRED_FIELDS.get(entry_type, ("title",))
            absent = [f for f in required if not fields.get(f)]
            if absent:
                incomplete.append(f"{_rel(root, bib)}: {key} lacks {', '.join(absent)}")
            doi = fields.get("doi", "")
            doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi, flags=re.IGNORECASE)
            if doi and not _DOI.match(doi):
                bad_doi.append(f"{_rel(root, bib)}: {key} doi = {fields['doi']}")
        if incomplete:
            self._add(
                title=f"{len(incomplete)} cited reference(s) lack required fields",
                severity="minor",
                description="A reference without author, title or year cannot be traced.",
                evidence=incomplete[:20],
                recommendation="Complete the entries from the publisher's record.",
            )
        if bad_doi:
            self._add(
                title=f"{len(bad_doi)} DOI(s) are malformed",
                severity="minor",
                description="A DOI has the form 10.<registrant>/<suffix>.",
                evidence=bad_doi[:20],
                recommendation="Correct the DOI, or remove the field.",
            )

    def _comments(self, root: Path, sources: list[Path]) -> None:
        private: list[str] = []
        for source in sources:
            for line_no, line in enumerate(source.read_text("utf-8", "replace").splitlines(), 1):
                comment = _comment_of(line)
                if comment and _PRIVATE.search(comment):
                    private.append(f"{_rel(root, source)}:{line_no}: %{comment.strip()[:100]}")
        if private:
            self._add(
                title=f"{len(private)} comment(s) look private and would be published",
                severity="minor",
                description=(
                    "arXiv publishes the LaTeX source, comments included. These look "
                    "like notes to yourself or to co-authors."
                ),
                evidence=private[:20],
                recommendation=(
                    "Remove them before submitting, or strip every comment with "
                    "arxiv_latex_cleaner."
                ),
            )

    def _size(self, root: Path, package: Path) -> None:
        limit = self.config.max_package_mb
        total = 0
        for path in package.rglob("*"):
            if any(part in _SKIP_SIZE for part in path.relative_to(package).parts):
                continue
            if path.is_file():
                total += path.stat().st_size
        size_mb = total / 1_000_000
        if size_mb > limit:
            self._add(
                title=f"The package is {size_mb:.1f} MB; arXiv accepts at most {limit:g} MB",
                severity=self.config.severity_on_failure,
                description=f"Everything under {_rel(root, package)}/ counts towards the upload.",
                location=_rel(root, package),
                recommendation="Compress figures, or leave out files the build does not use.",
            )

    # -------------------------------------------------------------- helpers

    def _missing(
        self, root: Path, document: Path, body: str | None, offset: int | None, name: str, kind: str
    ) -> None:
        location = _rel(root, document)
        if body is not None and offset is not None:
            location += f":{body.count(chr(10), 0, offset) + 1}"
        self._add(
            title=f"Missing {kind}: {name}",
            severity=self.config.severity_on_failure,
            description=(
                f"{location} refers to {kind} `{name}`, which is not in the package. "
                "arXiv's build will fail or omit it."
            ),
            location=location,
            evidence=[f"{location} -> {name}"],
            recommendation="Add the file next to the main .tex, or correct the reference.",
        )

    def _add(self, **kwargs: object) -> None:
        self.findings.append(
            check_finding(self.name, len(self.findings) + 1, **kwargs)  # type: ignore[arg-type]
        )


def _comment_of(line: str) -> str | None:
    """The comment part of a LaTeX line, ignoring escaped percent signs."""
    match = re.search(r"(?<!\\)%", line)
    return line[match.end() :] if match else None


def _strip_comments(text: str) -> str:
    return "\n".join(re.split(r"(?<!\\)%", line, maxsplit=1)[0] for line in text.splitlines())


def _find(folder: Path, name: str, suffixes: tuple[str, ...]) -> Path | None:
    base = folder / name
    if base.is_file():
        return base
    for suffix in suffixes:
        candidate = folder / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _bib_entries(text: str) -> list[tuple[str, str, str]]:
    """(type, key, body) for each entry, splitting at the next entry."""
    matches = list(_BIB_ENTRY.finditer(text))
    entries = []
    for index, match in enumerate(matches):
        entry_type = match.group(1).lower()
        if entry_type in ("comment", "preamble", "string"):
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        entries.append((entry_type, match.group(2), text[match.end() : end]))
    return entries


def _fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in re.finditer(r"(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\"|\w+)", body):
        fields[match.group(1).lower()] = match.group(2).strip('{}"').strip()
    return fields


def _rel(root: Path, target: Path) -> str:
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.as_posix()
