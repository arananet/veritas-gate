"""Schema validation for findings and judge results."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from veritas.models import Finding, JudgeResult, max_severity, severity_rank


def test_severity_ordering() -> None:
    assert severity_rank("critical") > severity_rank("major") > severity_rank("minor")
    assert max_severity(["minor", "critical", "info"]) == "critical"
    assert max_severity([]) == "info"


def test_invalid_severity_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Finding(
            id="X-1",
            title="t",
            severity="catastrophic",  # type: ignore[arg-type]
            category="c",
            description="d",
        )


def test_confidence_is_bounded() -> None:
    with pytest.raises(ValidationError):
        Finding(
            id="X-1", title="t", severity="minor", category="c", description="d", confidence=1.5
        )


def test_single_evidence_string_is_coerced() -> None:
    finding = Finding(
        id="X-1",
        title="t",
        severity="minor",
        category="c",
        description="d",
        evidence="only one line",  # type: ignore[arg-type]
    )
    assert finding.evidence == ["only one line"]
    assert finding.has_evidence


def test_finding_without_evidence_can_be_discounted() -> None:
    finding = Finding(
        id="X-1", title="t", severity="major", category="c", description="d", confidence=0.8
    )
    assert not finding.has_evidence
    assert finding.discounted(0.6).confidence == pytest.approx(0.48)


def test_judge_result_defaults_are_empty_not_none() -> None:
    result = JudgeResult(judge="methodology")
    assert result.findings == []
    assert result.claims == []
    assert result.status == "pass"
