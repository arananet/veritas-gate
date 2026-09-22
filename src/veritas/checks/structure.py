"""Structural checks that need no subprocess: required files and sections."""

from __future__ import annotations

import re
import time

from veritas.artifacts.base import Artifact
from veritas.checks.base import check_finding
from veritas.config import CheckConfig
from veritas.models.evaluation import CheckResult
from veritas.models.finding import Finding


class RequiredPathsCheck:
    """Assert that every configured path exists inside the artifact."""

    def __init__(self, name: str, config: CheckConfig) -> None:
        self.name = name
        self.config = config

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        missing = [
            path for path in self.config.required_paths if not (artifact.root / path).exists()
        ]
        duration = round(time.monotonic() - started, 3)
        if not self.config.required_paths:
            return CheckResult(
                check=self.name,
                status="skipped",
                summary="no required_paths configured",
                duration_seconds=duration,
            )
        if not missing:
            return CheckResult(
                check=self.name,
                status="pass",
                summary=f"all {len(self.config.required_paths)} required paths present",
                duration_seconds=duration,
            )
        return CheckResult(
            check=self.name,
            status="fail",
            summary=f"{len(missing)} required path(s) missing",
            findings=[
                check_finding(
                    self.name,
                    index,
                    title=f"Missing required path: {path}",
                    severity=self.config.severity_on_failure,
                    description=f"The artifact does not contain '{path}'.",
                    location=path,
                    evidence=[f"'{path}' was not found under {artifact.root}"],
                    recommendation=f"Add '{path}' to the artifact.",
                )
                for index, path in enumerate(missing, start=1)
            ],
            duration_seconds=duration,
        )


class RequiredSectionsCheck:
    """Assert that a target document contains every configured section heading."""

    def __init__(self, name: str, config: CheckConfig) -> None:
        self.name = name
        self.config = config

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        segments = artifact.segments()
        if self.config.target:
            segments = [item for item in segments if item.path == self.config.target]
        haystack = "\n".join(item.text for item in segments).lower()
        duration = round(time.monotonic() - started, 3)

        if not self.config.required_sections:
            return CheckResult(
                check=self.name,
                status="skipped",
                summary="no required_sections configured",
                duration_seconds=duration,
            )
        if not haystack.strip():
            return CheckResult(
                check=self.name,
                status="fail",
                summary=f"target '{self.config.target or 'artifact'}' has no readable text",
                findings=[
                    check_finding(
                        self.name,
                        1,
                        title="Section check target is unreadable",
                        severity=self.config.severity_on_failure,
                        description=(
                            f"No readable text was found for "
                            f"'{self.config.target or 'the artifact'}'."
                        ),
                        location=self.config.target,
                    )
                ],
                duration_seconds=duration,
            )

        missing = [
            section
            for section in self.config.required_sections
            if not re.search(rf"\b{re.escape(section.lower())}\b", haystack)
        ]
        if not missing:
            return CheckResult(
                check=self.name,
                status="pass",
                summary=f"all {len(self.config.required_sections)} sections present",
                duration_seconds=duration,
            )
        return CheckResult(
            check=self.name,
            status="fail",
            summary=f"{len(missing)} required section(s) missing",
            findings=[
                check_finding(
                    self.name,
                    index,
                    title=f"Missing required section: {section}",
                    severity=self.config.severity_on_failure,
                    description=f"No '{section}' section was found in the artifact.",
                    location=self.config.target,
                    evidence=[f"'{section}' does not appear as a heading or section title."],
                    recommendation=f"Add a '{section}' section.",
                )
                for index, section in enumerate(missing, start=1)
            ],
            duration_seconds=duration,
        )


class ContentPatternsCheck:
    """Assert that a target's text contains, or avoids, configured patterns.

    The cheap half of archival and citability review: whether a manuscript
    names a DOI, whether it cites a repository by commit rather than by branch,
    whether a data availability statement exists at all. A regular expression
    settles each of those, so no model is asked and the check costs nothing.
    Whether the DOI is the *right* one is a judgement, and belongs to a judge.
    """

    def __init__(self, name: str, config: CheckConfig) -> None:
        self.name = name
        self.config = config

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        configured = self.config.required_patterns or self.config.forbidden_patterns
        if not configured:
            return CheckResult(
                check=self.name,
                status="skipped",
                summary="no patterns configured",
                duration_seconds=_since(started),
            )

        segments = artifact.segments()
        if self.config.target:
            segments = [item for item in segments if item.path == self.config.target]
        text = "\n".join(item.text for item in segments)

        if not text.strip():
            target = self.config.target or "the artifact"
            return CheckResult(
                check=self.name,
                status="error",
                summary=f"target '{target}' has no readable text",
                findings=[
                    check_finding(
                        self.name,
                        1,
                        title="Pattern check target is unreadable",
                        severity="info",
                        description=(
                            f"No readable text was found for '{target}', so the "
                            "configured patterns could not be checked. A check that "
                            "cannot run must not be read as a check that passed."
                        ),
                        location=self.config.target,
                    )
                ],
                duration_seconds=_since(started),
            )

        flags = 0 if self.config.case_sensitive else re.IGNORECASE
        findings: list[Finding] = []
        index = 0

        for required in self.config.required_patterns:
            if re.search(required.pattern, text, flags):
                continue
            index += 1
            findings.append(
                check_finding(
                    self.name,
                    index,
                    title=f"Missing from the artifact: {required.label()}",
                    severity=self.config.severity_on_failure,
                    description=(
                        f"Nothing in {self.config.target or 'the artifact'} matches "
                        f"the expected pattern `{required.pattern}`."
                    ),
                    location=self.config.target,
                    evidence=[f"no match for `{required.pattern}`"],
                    recommendation=f"Add {required.label()}.",
                )
            )

        for forbidden in self.config.forbidden_patterns:
            matches = _matching_lines(text, forbidden.pattern, flags)
            if not matches:
                continue
            index += 1
            findings.append(
                check_finding(
                    self.name,
                    index,
                    title=f"Present in the artifact: {forbidden.label()}",
                    severity=self.config.severity_on_failure,
                    description=(
                        f"{len(matches)} line(s) in {self.config.target or 'the artifact'} "
                        f"match `{forbidden.pattern}`, which this profile treats as a defect."
                    ),
                    location=self.config.target,
                    # The quoted lines are what makes this actionable: the reader
                    # needs to know where, not only that.
                    evidence=[f"line {number}: {line}" for number, line in matches[:10]],
                    recommendation=f"Remove or replace {forbidden.label()}.",
                )
            )

        checked = len(self.config.required_patterns) + len(self.config.forbidden_patterns)
        if findings:
            return CheckResult(
                check=self.name,
                status="fail",
                summary=f"{len(findings)} of {checked} pattern(s) did not hold",
                findings=findings,
                duration_seconds=_since(started),
            )
        return CheckResult(
            check=self.name,
            status="pass",
            summary=f"all {checked} pattern(s) hold",
            duration_seconds=_since(started),
        )


def _matching_lines(text: str, pattern: str, flags: int) -> list[tuple[int, str]]:
    compiled = re.compile(pattern, flags)
    return [
        (number, line.strip())
        for number, line in enumerate(text.splitlines(), start=1)
        if compiled.search(line)
    ]


def _since(started: float) -> float:
    return round(time.monotonic() - started, 3)
