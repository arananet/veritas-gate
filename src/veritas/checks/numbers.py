"""Numeric traceability: every number in the paper must come from somewhere.

A paper reported 135 gate calls over 350 retrievals as a gate cost; the gated
denominator was 100. The number was real, the division was wrong, and three
evaluations of LLM judges argued about it before anyone opened the run files.
A number a reader cannot trace to the evidence is where that kind of error
hides, so this check asks, for every number in the manuscript, whether the
supplied evidence contains it -- directly, as a rounded value, as a percentage,
or as the ratio of two numbers the evidence does contain.

It does not decide that a traced number is used correctly. It finds the ones
nobody could check at all, which is where a judge, or the author, should look.
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

# A number in prose: 350, 1,350, 0.3857, 14.9%, -2, 52/350.
_NUMBER = re.compile(r"(?<![\w.#/@-])(-?\d{1,3}(?:,\d{3})+|-?\d+(?:\.\d+)?)(%?)(?![\w.]*[A-Za-z_])")
_RATIO = re.compile(r"(?<![\w.])(\d+)\s*/\s*(\d+)(?![\w.])")
_SOURCE_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?")

# Text that carries digits but no claim: code, links, identifiers, citations.
_STRIP = [
    re.compile(r"```.*?```", re.S),
    re.compile(r"`[^`\n]*`"),
    re.compile(r"\\(?:texttt|nolinkurl|url|href|ref|label|cite[a-z]*|eqref)\*?\{[^}]*\}"),
    re.compile(r"https?://\S+"),
    re.compile(r"\]\([^)]*\)"),
    re.compile(r"\b[0-9a-f]{7,40}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:]+Z?)?\b"),
    re.compile(r"\b\d{8}T\d{6}Z\b"),
    re.compile(r"\b10\.\d{4,9}/\S+"),
    re.compile(r"\bv?\d+\.\d+\.\d+(?:[-+.\w]*)?\b"),
    re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b"),  # CB-VAL-003, H1a-style ids
    re.compile(r"^\s*#+.*$", re.M),  # headings
    re.compile(r"^---\n.*?\n---\n", re.S),  # front matter
]

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
_DATE = re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTHS})\s+\d{{4}}\b|\b(?:{_MONTHS})\s+\d{{4}}\b")
_SECTION_REF = re.compile(
    r"\b(?:Section|Sec\.|Table|Figure|Fig\.|Appendix|Equation|Eq\.|RQ|H|C|R|S|D|§)~?\s*\d+(?:\.\d+)*",
    re.IGNORECASE,
)


class NumericTraceabilityCheck:
    """Report manuscript numbers that no supplied evidence source contains."""

    def __init__(self, name: str, config: CheckConfig) -> None:
        self.name = name
        self.config = config

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        target = self.config.target
        sources = list(self.config.sources)
        if not target or not sources:
            return CheckResult(
                check=self.name,
                status="skipped",
                summary="set target (the manuscript) and sources (evidence files)",
                duration_seconds=_since(started),
            )
        manuscript = artifact.root / target
        if not manuscript.is_file():
            return CheckResult(
                check=self.name,
                status="error",
                summary=f"{target} does not exist",
                duration_seconds=_since(started),
            )

        values, files = _source_values(artifact.root, sources)
        if not files:
            return CheckResult(
                check=self.name,
                status="error",
                summary="no evidence source matched the configured patterns",
                duration_seconds=_since(started),
            )

        ignore = [re.compile(p) for p in self.config.ignore_numbers]
        minimum = self.config.min_value
        untraced: dict[str, list[str]] = {}
        checked = 0
        numbers = _manuscript_numbers(manuscript.read_text(encoding="utf-8", errors="replace"))
        # Denominators a reader would recognise: integers the paper itself states.
        stated = {
            value for _, _, value, decimals, percent in numbers if not decimals and not percent
        }
        for line_no, token, value, decimals, percent in numbers:
            if abs(value) < minimum and decimals == 0 and not percent:
                continue  # small counts: "three scenarios", "step 2"
            if any(p.fullmatch(token) for p in ignore):
                continue
            checked += 1
            if not _traced(value, decimals, percent, values, stated):
                untraced.setdefault(token, []).append(f"{target}:{line_no}")

        findings: list[Finding] = []
        for index, (token, where) in enumerate(sorted(untraced.items()), start=1):
            findings.append(
                check_finding(
                    self.name,
                    index,
                    title=f"Number not traceable to the evidence: {token}",
                    severity=self.config.severity_on_failure,
                    description=(
                        f"{token} appears in the manuscript ({len(where)} time(s)) but "
                        f"no supplied evidence source ({len(files)} file(s)) contains it, "
                        "as a value, a rounding, a percentage, or a ratio of two of its "
                        "numbers. A reader cannot check where it came from."
                    ),
                    location=where[0],
                    evidence=where[:10],
                    recommendation=(
                        "Point the number at the evidence that produces it: add that file "
                        "to this check's sources, or compute it with a declared script and "
                        "supply its output. If it is not a result (a parameter, a count "
                        "from the text), add it to ignore_numbers."
                    ),
                )
            )
            if index >= 50:
                break

        status: Literal["pass", "fail"] = "fail" if findings else "pass"
        summary = (
            f"{checked} number(s) traced to {len(files)} evidence file(s)"
            if not findings
            else f"{len(untraced)} of {checked} number(s) not traceable to the evidence"
        )
        return CheckResult(
            check=self.name,
            status=status,
            summary=summary,
            findings=findings,
            duration_seconds=_since(started),
        )


def _manuscript_numbers(text: str) -> list[tuple[int, str, float, int, bool]]:
    """(line, token, value, decimals, is_percent) for each claim-like number."""
    found: list[tuple[int, str, float, int, bool]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw
        if line.lstrip().startswith(("%", "\\begin", "\\end", "\\usepackage", "\\documentclass")):
            continue
        line = _DATE.sub(" ", line)
        line = _SECTION_REF.sub(" ", line)
        for pattern in _STRIP:
            line = pattern.sub(" ", line)
        line = line.replace("\\%", "%").replace("\\,", "")
        for match in _NUMBER.finditer(line):
            token = match.group(1) + match.group(2)
            digits = match.group(1).replace(",", "")
            try:
                value = float(digits)
            except ValueError:
                continue
            decimals = len(digits.split(".", 1)[1]) if "." in digits else 0
            found.append((line_no, token, value, decimals, bool(match.group(2))))
    return found


def _source_values(root: Path, patterns: list[str]) -> tuple[set[float], list[Path]]:
    values: set[float] = set()
    files: list[Path] = []
    for pattern in patterns:
        matches = [root / pattern] if not any(c in pattern for c in "*?[") else root.glob(pattern)
        for match in matches:
            candidates = sorted(match.rglob("*")) if match.is_dir() else [match]
            for path in candidates:
                if not path.is_file() or path.stat().st_size > 50_000_000:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                files.append(path)
                for token in _SOURCE_NUMBER.findall(text.replace(",", "")):
                    try:
                        values.add(float(token))
                    except ValueError:
                        continue
    return values, files


def _close(value: float, candidate: float, decimals: int) -> bool:
    """Is ``value`` ``candidate`` as written with ``decimals`` places?"""
    tolerance: float = 0.5 * 10.0 ** (-decimals) + 1e-9
    return bool(abs(value - candidate) <= tolerance)


def _traced(
    value: float, decimals: int, percent: bool, values: set[float], stated: set[float]
) -> bool:
    targets = [value]
    if percent:
        targets.append(value / 100)
    else:
        targets.append(value * 100)  # a fraction written where the evidence has a percent
    for target in targets:
        places = decimals + (2 if percent and target != value else 0)
        if any(_close(target, v, places) for v in values):
            return True
    # A ratio of two evidence numbers whose denominator the paper states:
    # 1.35 = 135/100, 14.9% = 52/350. Unrestricted, almost any decimal is a
    # ratio of two integers somewhere in a large evidence set.
    if decimals == 0 and not percent:
        return False
    integers = sorted(d for d in stated if d in values and d > 1)
    ratio = value / 100 if percent else value
    places = decimals + (2 if percent else 0)
    tolerance = 0.5 * 10 ** (-places) + 1e-9
    for denominator in integers:
        numerator = ratio * denominator
        nearest = round(numerator)
        close = abs(nearest / denominator - ratio) <= tolerance
        if nearest in values and close and nearest != 0:
            return True
    return False


def _since(started: float) -> float:
    return round(time.monotonic() - started, 3)
