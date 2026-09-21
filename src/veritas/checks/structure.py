"""Structural checks that need no subprocess: required files and sections."""

from __future__ import annotations

import re
import time

from veritas.artifacts.base import Artifact
from veritas.checks.base import check_finding
from veritas.config import CheckConfig
from veritas.models.evaluation import CheckResult


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
