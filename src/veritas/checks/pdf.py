"""The PDF a reader will download, and the venue it is going to.

Two checks read the built PDF rather than its source:

* pdf-inspection: unresolved references (??), replacement characters,
  non-embedded fonts, missing metadata, and the TeX log's overfull boxes and
  undefined references -- the last things wrong before a submission.
* venue: the rules of the venue named in the config. For a double-blind venue,
  every identity term (from CITATION.cff and the git remote) searched in the
  PDF text, its metadata and the LaTeX source; the template package; the
  appendix after the references; the main-text page count; a broader impact
  statement where the venue asks for one.
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
from veritas.models.evaluation import CheckResult
from veritas.models.finding import Finding
from veritas.venue import find_terms, identity_terms, pdf_info, pdf_text, rules_for


class _Base:
    def __init__(self, name: str, config: CheckConfig) -> None:
        self.name = name
        self.config = config
        self.findings: list[Finding] = []

    def _add(
        self,
        title: str,
        severity: str,
        description: str,
        location: str | None,
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

    def _done(self, started: float, ok_summary: str) -> CheckResult:
        status: Literal["pass", "fail"] = "fail" if self.findings else "pass"
        return CheckResult(
            check=self.name,
            status=status,
            summary=ok_summary if not self.findings else f"{len(self.findings)} problem(s)",
            findings=self.findings,
            duration_seconds=round(time.monotonic() - started, 3),
        )


def _skip(name: str, started: float, why: str) -> CheckResult:
    return CheckResult(
        check=name,
        status="skipped",
        summary=why,
        duration_seconds=round(time.monotonic() - started, 3),
    )


class PdfInspectionCheck(_Base):
    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        self.findings = []
        if not self.config.target:
            return _skip(self.name, started, "set target to the built PDF")
        pdf = artifact.root / self.config.target
        if not pdf.is_file():
            return _skip(self.name, started, f"{self.config.target} does not exist")
        text = pdf_text(pdf)
        if text is None:
            return _skip(self.name, started, "pdftotext (poppler-utils) is not installed")
        where = self.config.target

        unresolved = [line.strip()[:100] for line in text.splitlines() if "??" in line]
        if unresolved:
            self._add(
                "Unresolved references or citations (??) in the PDF",
                self.config.severity_on_failure,
                "LaTeX prints ?? where a \\ref or \\cite did not resolve.",
                where,
                "Rebuild with the bibliography and all passes, or fix the label.",
                unresolved[:10],
            )
        if "\ufffd" in text:
            self._add(
                "Replacement characters in the PDF text",
                "minor",
                "Some glyphs could not be encoded; readers and search see garbage.",
                where,
                "Use a font with the missing glyphs, or replace the characters.",
            )
        fonts = _not_embedded(pdf)
        if fonts:
            self._add(
                "Fonts not embedded in the PDF",
                self.config.severity_on_failure,
                "Venues and archives reject PDFs whose fonts are not embedded.",
                where,
                "Build with a TeX engine that embeds all fonts.",
                fonts[:10],
            )
        info = pdf_info(pdf)
        if not self.config.anonymous and not info.get("Title"):
            self._add(
                "The PDF has no Title metadata",
                "minor",
                "Search engines and reference managers read the title from metadata.",
                where,
                "Set it with hyperref (pdftitle).",
            )
        log = pdf.with_suffix(".log")
        if log.is_file():
            body = log.read_text(encoding="utf-8", errors="replace")
            overfull = re.findall(r"Overfull \\hbox \(([\d.]+)pt too wide\).*?lines? (\d+)", body)
            wide = [f"line {line}: {pt}pt" for pt, line in overfull if float(pt) > 1.0]
            if wide:
                self._add(
                    f"{len(wide)} line(s) run into the margin",
                    "minor",
                    "Overfull boxes print text past the margin.",
                    log.name,
                    "Reword, allow breaking in long identifiers, or shorten them.",
                    wide[:10],
                )
            undefined = re.findall(r"(?:Reference|Citation) `([^']+)' .*undefined", body)
            if undefined:
                self._add(
                    "Undefined references or citations in the TeX log",
                    self.config.severity_on_failure,
                    "These labels or keys were never defined.",
                    log.name,
                    "Define them, or rebuild after running BibTeX.",
                    sorted(set(undefined))[:10],
                )
        return self._done(started, f"{info.get('Pages', '?')} page(s), no problems found")


def _not_embedded(pdf: Path) -> list[str]:
    try:
        output = subprocess.run(
            ["pdffonts", str(pdf)], capture_output=True, text=True, timeout=60, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    missing = []
    for line in output.splitlines()[2:]:
        parts = line.split()
        # name type encoding emb sub uni object ID: 'emb' is the 5th-from-last field
        if len(parts) >= 6 and parts[-5] == "no":
            missing.append(parts[0])
    return missing


class VenueCheck(_Base):
    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        self.findings = []
        venue = self.config.venue
        if not venue:
            return _skip(self.name, started, "set venue (tmlr, arxiv, preprint)")
        try:
            rules = rules_for(str(venue))
        except ValueError as exc:
            return CheckResult(check=self.name, status="error", summary=str(exc))
        root = artifact.root
        pdf = root / self.config.target if self.config.target else None
        source_path = self.config.source
        source = root / source_path if source_path else None
        text = pdf_text(pdf) if pdf and pdf.is_file() else None
        latex = (
            source.read_text(encoding="utf-8", errors="replace")
            if source and source.is_file()
            else ""
        )

        if rules.anonymous:
            terms = identity_terms(root, list(self.config.identity_terms))
            places: list[str] = []
            if text:
                places += [f"PDF text: {t}" for t in find_terms(text, terms)]
            if pdf and pdf.is_file():
                meta = " ".join(pdf_info(pdf).values())
                places += [f"PDF metadata: {t}" for t in find_terms(meta, terms)]
            if latex:
                places += [f"LaTeX source: {t}" for t in find_terms(latex, terms)]
            if places:
                self._add(
                    f"{rules.name}: the submission identifies the author",
                    "critical",
                    f"{rules.name} reviews double-blind and rejects non-anonymous "
                    "submissions without review. Identity terms come from CITATION.cff, "
                    "the git remote and identity_terms.",
                    self.config.target,
                    "Remove or replace each term; cite your own work in the third person; "
                    "link code through an anonymous mirror.",
                    places[:20],
                )
            if re.search(r"(?i)\b(?:the author'?s|my (?:own )?(?:work|proposal))\b", latex):
                self._add(
                    "First-person self-reference in the source",
                    "major",
                    "Phrases like \"the author's\" reveal that a cited work is the author's.",
                    source_path,
                    "Cite your own work in the third person, as if it were someone else's.",
                )
        if rules.template_package and latex:
            match = re.search(rf"\\usepackage(\[[^\]]*\])?\{{{rules.template_package}\}}", latex)
            if not match:
                self._add(
                    f"{rules.name} template not used",
                    "critical",
                    f"The source does not load \\usepackage{{{rules.template_package}}}; the "
                    "venue's stylefile is mandatory.",
                    source_path,
                    f"Use the official {rules.name} template.",
                )
            elif match.group(1) and rules.anonymous:
                self._add(
                    f"{rules.name} template loaded with options {match.group(1)}",
                    "critical",
                    "Options such as [preprint] or [accepted] de-anonymise the submission.",
                    source_path,
                    f"Use \\usepackage{{{rules.template_package}}} without options.",
                )
        if rules.appendix_after_references and latex:
            appendix = latex.find("\\appendix")
            bibliography = max(
                latex.find("\\bibliography{"), latex.find("\\begin{thebibliography}")
            )
            if appendix != -1 and bibliography != -1 and appendix < bibliography:
                self._add(
                    "Appendix comes before the references",
                    "major",
                    f"{rules.name} places appendices after the references.",
                    source_path,
                    "Move \\appendix and what follows after \\bibliography.",
                )
        if rules.main_page_limit and text:
            pages = text.split("\f")
            main = next(
                (
                    i
                    for i, page in enumerate(pages, start=1)
                    if re.search(r"^\s*References\s*$", page, re.M)
                ),
                None,
            )
            if main is not None and main - 1 > rules.main_page_limit:
                self._add(
                    f"Main text runs to {main - 1} pages (regular limit {rules.main_page_limit})",
                    "info",
                    f"Choose '{rules.name} long submission', which is reviewed more slowly, "
                    "or shorten the main text.",
                    self.config.target,
                    "Move detail to the appendix, or declare a long submission.",
                )
        if (
            rules.broader_impact
            and (latex or text)
            and not re.search(r"(?i)broader impact", latex or text or "")
        ):
            self._add(
                "No broader impact statement",
                "minor",
                f"{rules.name} asks for one when work could cause harm, and reviewers look for it.",
                source_path,
                "Add a short Broader Impact Statement.",
            )
        return self._done(started, f"{rules.name}: no venue problems found")
