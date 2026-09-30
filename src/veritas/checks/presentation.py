"""Presentation: what a reader sees before they weigh a single claim.

A manuscript revised by agents kept its revision log in the paper: "human
review remains pending", "this session is an agent-assisted local rerun", the
exit code of a crashed TeX engine, a 40-character commit hash six times, a
three-paragraph abstract ending in a provenance note. Each is harmless alone;
together they read as unreviewed machine output, which venues now screen for.
These are patterns, so a check finds them for nothing and a judge is left the
questions that need judgement.

Also reported: figures the text never cites, and a paper with no figure at all.

The front matter is where a paper is triaged. A preprint that got read
(SkillGym, 2026) put its headline numbers in the abstract, a comparison figure
on page 2, three contribution bullets in the introduction and a table against
fourteen prior benchmarks; a desk-rejected one put its limitations there
instead. Each of those four is a pattern, so this check reports their absence.
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
    (
        r"(?i)\bat the user'?s request\b|\bthe user asked\b"
        r"|\bthe user'?s (?:description|instructions?|prompt|request)\b",
        "conversation",
    ),
    (r"(?i)\bas an AI\b|\bI (?:have|will) (?:now )?(?:updated|added|removed)\b", "assistant voice"),
]

# A sentence that tells the reader what the work does not show or has not done.
# Bare negation is not counted: "the core is not sufficient" is a result, and a
# negative result stated plainly is good presentation. Calibrated on three real
# abstracts: two desk-rejected or flagged drafts (0.36, 0.37) and one accepted
# preprint whose negatives are findings (0.11).
_DISCLAIMER = re.compile(
    r"""(?ix)
    \b(?:does|do|did|is|are|was|were|can|could)\s*(?:not|n't)\s+
        (?:establish|prove|show|estimate|measure|claim|isolate|model|demonstrate
          |mean|imply|constitute|cover|evaluate|test|replicate|generali[sz]e)
    | \bnot\s+(?:evaluated|measured|executed|tested|claimed|established|assessed
          |verified|demonstrated|examined|studied|intrinsic
          |a\s+(?:proof|ranking|replication|measurement|guarantee)
          |\w+\s+measurements)
    | \bno\s+(?:LLM|experiment|performance|user\s+study|independent|fresh|security\s+review)
    | \bremains?\s+(?:pending|unresolved|unexecuted|untested)
    | \bpending\s+(?:human\s+)?review
    | \brather\s+than\s+(?:\w+\s+){0,3}(?:empirically|demonstrated|measured|proven)
    | \b(?:concern|apply\s+to|limited\s+to)\s+(?:the|these)\s+(?:tested|evaluated|inspected)
    | \bwithin-scenario\b | \bonly\s+(?:clean|one|a\s+single)\b
    | \bwas\s+blocked\b | \bretrospective\b
    """
)
_SENTENCE = re.compile(r"(?<=[.;])\s+(?=[A-Z(])")

# A number that reports a result: a percentage, a difference in points, a
# ratio, a decimal, or a comparison ("from 39 to 58", "51.5 versus 50.1").
_RESULT_NUMBER = re.compile(
    r"(?i)\b\d+(?:\.\d+)?\s*(?:%|pp\b|percentage points?|points?\b|\u00d7|-?fold\b)"
    r"|\b\d+\.\d+\b|\b(?:from|to|vs\.?|versus|of)\s+\d"
)
_POSITIONING = re.compile(r"(?i)compar|prior work|existing|related|versus|\bvs\.?\s|position")
_MD_TABLE_CAPTION = re.compile(r"(?im)^\s*(?:Table\s*\d*\s*[:.]|:\s+\S)(.*)$")
_TEX_TABLE = re.compile(r"\\begin\{table\*?\}(.*?)\\end\{table\*?\}", re.S)
_LIST_ITEM = re.compile(r"(?m)^\s*(?:[-*+]\s|\d+[.)]\s)|\\item\b")
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
        self._front_matter(target, text)
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
        self._hedging(target, body)
        paragraphs = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
        words = len(re.findall(r"\w+", body))
        limit = self.config.abstract_max_words
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

    def _hedging(self, target: str, body: str) -> None:
        """Report an abstract whose disclaimers crowd out its result.

        Each limitation may be correct; stated in the abstract one after another
        they bury the finding, and an editor triaging on the abstract reads the
        work as unfinished. Advisory: a count cannot tell a needed scope
        statement from a redundant one, so this never blocks on its own.
        """
        sentences = [s for s in _SENTENCE.split(" ".join(body.split())) if s.strip()]
        if len(sentences) < 3:
            return
        flagged = [s for s in sentences if _DISCLAIMER.search(s)]
        ratio = len(flagged) / len(sentences)
        limit = self.config.abstract_max_disclaimer_ratio
        if len(flagged) >= 3 and ratio > limit:
            self._add(
                f"Abstract spends {len(flagged)} of {len(sentences)} sentences on what "
                f"the work does not show ({ratio:.0%}, limit {limit:.0%})",
                "minor",
                "The limitations may all be true. Concentrated in the abstract they hide "
                "the result, and a reader triaging on the abstract takes the work for "
                "unfinished. Scope belongs in the sentence that states the result; the "
                "rest belongs in Limitations, once each.",
                target,
                "State each main result with its scope in one sentence ('in the N "
                "evaluated scenarios, X excluded Y'). Keep at most one sentence of "
                "limitations in the abstract and move the others to Limitations. Do "
                "not delete a limitation; relocate it.",
                [f"{target}: {s[:120]}" for s in flagged[:10]],
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
        if count == 0 and self.config.require_figures:
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

    def _front_matter(self, target: str, text: str) -> None:
        """What an editor sees first: a number, contributions, positioning, a figure."""
        abstract = _section(text, r"abstract")
        if (
            abstract is not None
            and self.config.abstract_requires_number
            and not _RESULT_NUMBER.search(abstract)
        ):
            self._add(
                "Abstract reports no quantitative result",
                "minor",
                "An editor triaging on the abstract looks for what was found and how "
                "much. Without a number the finding cannot be repeated after one read.",
                target,
                "State the main result with its number and comparison ('X raises Y from "
                "A to B on N tasks'), using only numbers the paper reports. For a formal "
                "result, state the theorem; set abstract_requires_number: false.",
            )
        intro = _section(text, r"introduction")
        if (
            intro is not None
            and not _LIST_ITEM.search(intro)
            and not re.search(r"(?i)\bcontribut", intro)
        ):
            self._add(
                "Introduction does not list its contributions",
                "minor",
                "Accepted papers name their contributions in the introduction, usually "
                "as a short list, so a reader knows what to evaluate.",
                target,
                "End the introduction with two to four contribution bullets, each "
                "pointing to the section and result that supports it.",
            )
        related = _section(text, r"related\s+work|background|prior\s+work")
        captions = [m.group(1) for m in _MD_TABLE_CAPTION.finditer(text)]
        for block in _TEX_TABLE.findall(text):
            captions.extend(re.findall(r"\\caption\{([^}]*)", block))
        if (
            related is not None
            and self.config.require_positioning_table
            and not any(_POSITIONING.search(c) for c in captions)
        ):
            self._add(
                "No table positions the work against prior work",
                "minor",
                "Related work in prose leaves the reader to work out what is new. A "
                "table of prior work against the properties this work claims shows it "
                "in one look.",
                target,
                "Add a comparison table: rows are prior works cited in Related Work, "
                "columns the properties this paper claims, this work as the last row.",
            )
        first = _first_figure(text)
        limit = self.config.first_figure_within_words
        if first is not None and len(re.findall(r"\w+", text[:first])) > limit:
            self._add(
                f"First figure appears after more than {limit} words",
                "minor",
                "A figure near the start (the main result, or the method at a glance) "
                "is what most readers look at before deciding to read on.",
                target,
                "Move or add a main-result or overview figure to the first two pages, "
                "generated from the evidence.",
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


def _section(text: str, name: str) -> str | None:
    """Body of a Markdown or LaTeX section whose title matches ``name``."""
    markdown = re.search(
        rf"(?im)^#+\s*(?:\d+\.?\s*)?(?:{name})\b[^\n]*\n(.*?)(?=^#+\s|\Z)", text, re.S
    )
    if markdown:
        return markdown.group(1)
    if re.fullmatch(r"abstract", name):
        latex = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", text, re.S)
        return latex.group(1) if latex else None
    latex = re.search(
        rf"(?i)\\section\*?\{{(?:{name})[^}}]*\}}(.*?)(?=\\section|\\end\{{document\}}|\Z)",
        text,
        re.S,
    )
    return latex.group(1) if latex else None


def _first_figure(text: str) -> int | None:
    positions = [m.start() for m in (_MD_IMAGE.search(text), _TEX_FIGURE.search(text)) if m]
    return min(positions) if positions else None
