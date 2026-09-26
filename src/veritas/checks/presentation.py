"""Presentation: what a reader sees before they weigh a single claim.

A manuscript revised by agents kept its revision log in the paper: "human
review remains pending", "this session is an agent-assisted local rerun", the
exit code of a crashed TeX engine, a 40-character commit hash six times, a
three-paragraph abstract ending in a provenance note. Each is harmless alone;
together they read as unreviewed machine output, which venues now screen for.
These are patterns, so a check finds them for nothing and a judge is left the
questions that need judgement.

Also reported: figures the text never cites, and a paper with no figure at all.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from typing import Literal

from veritas.artifacts.base import Artifact
from veritas.checks.base import check_finding
from veritas.config import CheckConfig
from veritas.models.evaluation import CheckResult
from veritas.models.finding import Finding

# Process narration that belongs in an execution record, not a paper.
PROCESS_PATTERNS: list[tuple[str, str]] = [
    (
        r"(?i)\b(?:human|author|editorial|scientific)?\s*review\s+(?:remains\s+|is\s+)?pending",
        "review pending",
    ),
    (r"(?i)\blocal draft\b", "local draft"),
    (r"(?i)\bthis (?:session|run of the agent|agent-assisted)", "session narration"),
    (r"(?i)\bagent-assisted (?:local )?(?:rerun|record|session)", "agent-assisted record"),
    (r"\bEPERM\b|\bENOENT\b|\bpanicked\b|\bexit (?:code )?1\d\d\b", "tool failure"),
    (r"(?i)\btimed out after \d+\s*ms\b", "tool timeout"),
    (r"(?i)\b\d+ passed(?:,| and) \d+ failed\b", "test tally"),
    (r"(?i)\bat the user'?s request\b|\bthe user asked\b", "conversation"),
    (r"(?i)\bas an AI\b|\bI (?:have|will) (?:now )?(?:updated|added|removed)\b", "assistant voice"),
]

_HEX = re.compile(r"\b[0-9a-f]{20,64}\b")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)[^)]*\)(\{#([\w:.-]+)[^}]*\})?")
_TEX_FIGURE = re.compile(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", re.S)
_TEX_LABEL = re.compile(r"\\label\{([^}]+)\}")


class PresentationCheck:
    def __init__(self, name: str, config: CheckConfig) -> None:
        self.name = name
        self.config = config
        self.findings: list[Finding] = []

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        self.findings = []
        target = self.config.target
        if not target or not (artifact.root / target).is_file():
            return CheckResult(
                check=self.name,
                status="skipped",
                summary="set target to the manuscript",
                duration_seconds=round(time.monotonic() - started, 3),
            )
        text = (artifact.root / target).read_text(encoding="utf-8", errors="replace")
        self._process(target, text)
        self._repeated_identifiers(target, text)
        self._abstract(target, text)
        self._figures(target, text)
        status: Literal["pass", "fail"] = "fail" if self.findings else "pass"
        summary = (
            "no presentation problems found"
            if not self.findings
            else f"{len(self.findings)} presentation problem(s)"
        )
        return CheckResult(
            check=self.name,
            status=status,
            summary=summary,
            findings=self.findings,
            duration_seconds=round(time.monotonic() - started, 3),
        )

    def _process(self, target: str, text: str) -> None:
        hits: list[str] = []
        kinds: set[str] = set()
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pattern, kind in PROCESS_PATTERNS:
                if re.search(pattern, line):
                    hits.append(f"{target}:{line_no}: {line.strip()[:120]}")
                    kinds.add(kind)
                    break
        if hits:
            self._add(
                f"Process narration in the manuscript ({', '.join(sorted(kinds))})",
                self.config.severity_on_failure,
                f"{len(hits)} line(s) narrate how the paper was produced rather than what "
                "it shows. Tool failures, test tallies, session notes and review status "
                "belong in an execution record; in the paper they read as unreviewed "
                "machine output.",
                hits[0].rsplit(":", 1)[0],
                "Move these to the execution record (for example EXECUTION.md) and keep "
                "in the paper only what a reader needs to evaluate the claims.",
                hits[:15],
            )

    def _repeated_identifiers(self, target: str, text: str) -> None:
        # A pinned URL carries its hash by necessity; count prose mentions only.
        prose = re.sub(r"https?://\S+", " ", text)
        counts = Counter(_HEX.findall(prose))
        repeated = [f"{value} ({n} times)" for value, n in counts.items() if n >= 3]
        if repeated:
            self._add(
                "Long identifiers repeated throughout the manuscript",
                "minor",
                "The same full hash appears three or more times. State it once, where "
                "provenance is described, and refer to it by a short form elsewhere.",
                target,
                "Keep the full identifier once (Reproducibility or Data availability).",
                repeated[:10],
            )

    def _abstract(self, target: str, text: str) -> None:
        body = None
        markdown = re.search(r"^#+\s*Abstract\s*\n(.*?)(?=^#+\s)", text, re.M | re.S)
        latex = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", text, re.S)
        if markdown:
            body = markdown.group(1)
        elif latex:
            body = latex.group(1)
        if body is None:
            return
        paragraphs = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
        words = len(re.findall(r"\w+", body))
        limit = int(getattr(self.config, "abstract_max_words", 300))
        if len(paragraphs) > 1 or words > limit:
            problems = []
            if len(paragraphs) > 1:
                problems.append(f"{len(paragraphs)} paragraphs")
            if words > limit:
                problems.append(f"{words} words (limit {limit})")
            self._add(
                f"Abstract is {' and '.join(problems)}",
                "minor",
                "Most venues require a one-paragraph abstract, and a long one hides the "
                "result a reader is looking for.",
                target,
                "One paragraph: the problem, what was done, the main result with its "
                "number, and its scope.",
            )

    def _figures(self, target: str, text: str) -> None:
        labels: list[str] = []
        count = 0
        for match in _MD_IMAGE.finditer(text):
            count += 1
            if match.group(3):
                labels.append(match.group(3))
        for block in _TEX_FIGURE.findall(text):
            count += 1
            labels.extend(_TEX_LABEL.findall(block))
        if count == 0 and getattr(self.config, "require_figures", True):
            self._add(
                "The manuscript has no figure",
                "minor",
                "Readers find a result faster in a figure than in prose or a dense table: "
                "a main-result figure, a diagram of the method, or a comparison.",
                target,
                "Add a main-result figure generated from the evidence, declared under "
                "`derived:` so it stays reproducible.",
            )
        for label in labels:
            uses = len(re.findall(re.escape(label), text)) - 1
            if uses <= 0:
                self._add(
                    f"Figure never referenced in the text: {label}",
                    "minor",
                    "Every figure should be cited where the text relies on it.",
                    target,
                    f"Reference it (\\ref{{{label}}} or @{label}) where it is discussed.",
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
