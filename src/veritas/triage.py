"""Who acts on a finding, and what a declared limitation weighs.

A flat list of findings left a person to say, every round, which ones are fixed
in the work, which in veritas.yaml, which are the author's to decide, and which
the work already acknowledges. Judges propose a disposition; these rules settle
the cases Veritas knows better than any judge.
"""

from __future__ import annotations

from typing import get_args

from veritas.models.finding import Disposition, Finding, severity_rank

DISPOSITIONS: tuple[str, ...] = get_args(Disposition)

# A limitation the work declares is weighed as minor at most. Never below the
# work's own honesty, never above it -- and never for a critical finding:
# acknowledging a fabricated result does not make it acceptable.
DECLARED_CEILING = "minor"


def declared(finding: Finding) -> Finding:
    """Cap a declared limitation, provided the judge quoted the declaration.

    Without quoted evidence a judge could launder any defect by calling it
    declared, so an unevidenced claim of declaration is not honoured.
    """
    if finding.disposition != "declared":
        return finding
    if not finding.has_evidence:
        return finding.model_copy(update={"disposition": "artifact"})
    if finding.severity == "critical":
        return finding
    if severity_rank(finding.severity) <= severity_rank(DECLARED_CEILING):
        return finding
    return finding.model_copy(
        update={"severity": DECLARED_CEILING, "original_severity": finding.severity}
    )


def settle(finding: Finding) -> Finding:
    """Apply the dispositions Veritas knows for certain, overriding any judge."""
    category = finding.category
    if category == "judge-error":
        # A failed judge is fixed by re-running with a working configuration.
        return finding.model_copy(update={"disposition": "configuration"})
    if category.startswith("check/") and category.endswith("/unsupplied"):
        # The document is right; artifact.paths does not include what it cites.
        return finding.model_copy(update={"disposition": "configuration"})
    if finding.disposition not in DISPOSITIONS:
        return finding.model_copy(update={"disposition": "artifact"})
    return finding


def normalise(value: str | None) -> Disposition:
    candidate = (value or "").strip().lower()
    return candidate if candidate in DISPOSITIONS else "artifact"  # type: ignore[return-value]


TRIAGE_RULES = """\
TRIAGE RULES:

- Before reporting, read the artifact's own limitations, threats-to-validity and
  scope statements.
- If the artifact already states a limitation explicitly, and states it no more
  weakly than the evidence requires, report it with disposition "declared" and
  quote the acknowledging passage in evidence. It will be weighed as minor. If the
  acknowledgement is weaker than the problem, or is contradicted elsewhere (for
  example the abstract or conclusion claims more than the limitation allows),
  report the contradiction with disposition "artifact" instead.
- Give every finding a disposition:
    "artifact"      the work itself must change (text, code, data, evidence);
    "configuration" the evaluation setup is wrong, not the work;
    "decision"      only the author can resolve it (an identifier, a licence,
                    a new experiment, a scope choice);
    "declared"      as above.
"""
