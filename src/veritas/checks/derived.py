"""Derived-file freshness and figure provenance, checked without running anything.

Reads veritas.lock.json, written by ``veritas build``, and compares it with
the files on disk:

* an input changed since the build: the output is stale;
* an output changed while its inputs did not: it was edited by hand;
* a declared derivation was never built: nothing records what produced it;
* a figure the manuscript shows that no derivation produces: nobody can
  regenerate it from the evidence.
"""

from __future__ import annotations

import re
import time
from pathlib import Path, PurePosixPath
from typing import Literal

from veritas.artifacts.base import Artifact
from veritas.checks.base import check_finding
from veritas.config import CheckConfig, Derivation
from veritas.derive import LOCK_NAME, expand, hashes, load_lock
from veritas.models.evaluation import CheckResult
from veritas.models.finding import Finding

_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
_INCLUDEGRAPHICS = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}")
_IMAGE_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".svg", ".eps")


class DerivedFreshnessCheck:
    def __init__(self, name: str, config: CheckConfig, derived: list[Derivation]) -> None:
        self.name = name
        self.config = config
        self.derived = derived
        self.findings: list[Finding] = []

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        self.findings = []
        root = artifact.root
        if not self.derived and not self.config.target:
            return CheckResult(
                check=self.name,
                status="skipped",
                summary="no derivations declared and no manuscript target",
                duration_seconds=round(time.monotonic() - started, 3),
            )
        lock = load_lock(root)
        for derivation in self.derived:
            self._compare(root, derivation, lock.get(derivation.id))
        if self.config.target:
            self._figures(root, self.config.target)

        status: Literal["pass", "fail"] = "fail" if self.findings else "pass"
        summary = (
            f"{len(self.derived)} derivation(s) fresh"
            if not self.findings
            else f"{len(self.findings)} derived-file problem(s)"
        )
        return CheckResult(
            check=self.name,
            status=status,
            summary=summary,
            findings=self.findings,
            duration_seconds=round(time.monotonic() - started, 3),
        )

    def _compare(self, root: Path, derivation: Derivation, entry: object) -> None:
        if not isinstance(entry, dict):
            self._add(
                f"Derivation '{derivation.id}' was never built through Veritas",
                "minor",
                f"No entry in {LOCK_NAME}, so nothing records which inputs produced "
                f"{', '.join(derivation.outputs)}.",
                derivation.outputs[0],
                "Run `veritas build` and commit veritas.lock.json.",
            )
            return
        recorded_inputs = entry.get("inputs") or {}
        recorded_outputs = entry.get("outputs") or {}
        current_inputs = hashes(root, expand(root, derivation.inputs))
        changed = sorted(
            path
            for path in set(recorded_inputs) | set(current_inputs)
            if recorded_inputs.get(path) != current_inputs.get(path)
        )
        current_outputs = hashes(root, list(recorded_outputs))
        missing = sorted(p for p in recorded_outputs if p not in current_outputs)
        edited = sorted(
            p
            for p in recorded_outputs
            if p in current_outputs and current_outputs[p] != recorded_outputs[p]
        )
        if missing:
            self._add(
                f"Derived output missing: {', '.join(missing)}",
                self.config.severity_on_failure,
                f"'{derivation.id}' recorded these outputs, which no longer exist.",
                missing[0],
                "Run `veritas build`.",
                missing,
            )
        if changed:
            self._add(
                f"Stale {derivation.kind}: '{derivation.id}' inputs changed since it was built",
                self.config.severity_on_failure,
                f"{len(changed)} input(s) differ from the build recorded in {LOCK_NAME}, so "
                f"{', '.join(derivation.outputs)} may no longer reflect them.",
                derivation.outputs[0],
                f"Run `veritas build --only {derivation.id}` and commit the result.",
                changed[:10],
            )
        elif edited:
            self._add(
                f"Derived {derivation.kind} edited by hand: {', '.join(edited)}",
                self.config.severity_on_failure,
                f"'{derivation.id}' inputs are unchanged but its output differs from the "
                "build: it was edited after being generated, so regenerating it would "
                "silently lose or change what the paper shows.",
                edited[0],
                "Put the change in the generating script, then `veritas build`.",
                edited,
            )

    def _figures(self, root: Path, target: str) -> None:
        manuscript = root / target
        if not manuscript.is_file():
            return
        text = manuscript.read_text(encoding="utf-8", errors="replace")
        produced = {
            path for derivation in self.derived for path in expand(root, derivation.outputs)
        }
        base = PurePosixPath(target).parent
        for reference in [*_MARKDOWN_IMAGE.findall(text), *_INCLUDEGRAPHICS.findall(text)]:
            if reference.startswith(("http://", "https://")):
                continue
            candidates = [reference]
            if not PurePosixPath(reference).suffix:
                candidates = [reference + s for s in _IMAGE_SUFFIXES]
            resolved = None
            for candidate in candidates:
                for rel in (base / candidate, PurePosixPath(candidate)):
                    normal = PurePosixPath(*[p for p in rel.parts if p != "."]).as_posix()
                    if (root / normal).is_file():
                        resolved = normal
                        break
                if resolved:
                    break
            if resolved is None or resolved in produced:
                continue
            self._add(
                f"Figure without provenance: {resolved}",
                "minor",
                "The manuscript shows this figure, but no declared derivation produces it, "
                "so a reader cannot regenerate it from the evidence.",
                resolved,
                "Declare the script that draws it under `derived:` (kind: figure) and "
                "run `veritas build`.",
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
