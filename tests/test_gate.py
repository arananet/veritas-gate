"""Gate engine rules. The gate is deterministic and never calls a model."""

from __future__ import annotations

from veritas.config import GatePolicy
from veritas.gate import evaluate_gate
from veritas.models import CheckResult, ClaimCoverage, Finding


def finding(fid: str, severity: str) -> Finding:
    return Finding(
        id=fid,
        title=f"{severity} issue",
        severity=severity,  # type: ignore[arg-type]
        category="test",
        description="d",
    )


def test_clean_run_passes() -> None:
    result = evaluate_gate([], [], GatePolicy())
    assert result.status == "PASS"
    assert result.exit_code == 0


def test_minor_findings_warn() -> None:
    result = evaluate_gate([finding("A-1", "minor")], [], GatePolicy())
    assert result.status == "PASS_WITH_WARNINGS"
    assert result.exit_code == 1


def test_minor_findings_do_not_warn_when_disabled() -> None:
    result = evaluate_gate([finding("A-1", "minor")], [], GatePolicy(warn_on_minor=False))
    assert result.status == "PASS"


def test_major_over_limit_requires_revision() -> None:
    findings = [finding("A-1", "major"), finding("A-2", "major")]
    result = evaluate_gate(findings, [], GatePolicy(max_major=0))
    assert result.status == "REVISE"
    assert result.exit_code == 2
    assert result.blocking_findings == ["A-1", "A-2"]


def test_major_within_limit_passes() -> None:
    result = evaluate_gate([finding("A-1", "major")], [], GatePolicy(max_major=1))
    assert result.status == "PASS"


def test_critical_fails_and_is_blocking() -> None:
    result = evaluate_gate([finding("A-1", "critical")], [], GatePolicy())
    assert result.status == "FAIL"
    assert result.exit_code == 3
    assert "A-1" in result.blocking_findings


def test_critical_survives_a_sea_of_info_findings() -> None:
    """A critical finding can never be diluted by counting."""
    findings = [finding(f"I-{i}", "info") for i in range(50)] + [finding("C-1", "critical")]
    result = evaluate_gate(findings, [], GatePolicy())
    assert result.status == "FAIL"
    assert "C-1" in result.blocking_findings


def test_required_check_missing_fails() -> None:
    result = evaluate_gate([], [], GatePolicy(require_checks=["tests"]))
    assert result.status == "FAIL"
    assert result.failed_checks == ["tests"]


def test_required_check_failing_fails() -> None:
    check = CheckResult(
        check="tests",
        status="fail",
        findings=[finding("CHECK-TESTS-001", "major")],
    )
    result = evaluate_gate([], [check], GatePolicy(require_checks=["tests"]))
    assert result.status == "FAIL"
    assert "CHECK-TESTS-001" in result.blocking_findings


def test_skipped_required_check_counts_as_passed() -> None:
    check = CheckResult(check="tests", status="skipped")
    result = evaluate_gate([], [check], GatePolicy(require_checks=["tests"]))
    assert result.status == "PASS"


def test_evidence_coverage_floor_triggers_revision() -> None:
    coverage = ClaimCoverage(total=10, major=10, verified=5, coverage=50.0)
    result = evaluate_gate([], [], GatePolicy(min_evidence_coverage=80.0), coverage)
    assert result.status == "REVISE"


def test_minor_limit_does_not_escalate_to_revise() -> None:
    findings = [finding(f"M-{i}", "minor") for i in range(5)]
    result = evaluate_gate(findings, [], GatePolicy(max_minor=2))
    assert result.status == "PASS_WITH_WARNINGS"
    assert any("exceed the limit" in reason for reason in result.reasons)


def test_a_run_where_every_judge_failed_can_never_pass() -> None:
    """Regression: an evaluation that did not happen was reported as PASS.

    With bad credentials every judge errors, produces no artifact findings, and
    the gate saw an empty list — so it approved a manuscript it never read.
    """
    result = evaluate_gate([], [], GatePolicy(), judge_errors=["methodology", "evidence"])
    assert result.status == "FAIL"
    assert result.exit_code == 3
    assert result.judge_errors == ["methodology", "evidence"]
    assert any("not fully evaluated" in reason for reason in result.reasons)


def test_a_single_judge_failure_still_blocks_the_gate() -> None:
    result = evaluate_gate([], [], GatePolicy(), judge_errors=["citations"])
    assert result.status == "FAIL"


def test_judge_errors_can_be_tolerated_explicitly() -> None:
    result = evaluate_gate([], [], GatePolicy(fail_on_judge_error=False), judge_errors=["x"])
    assert result.status == "PASS"
    assert result.judge_errors == ["x"]


def test_no_judge_errors_leaves_the_verdict_untouched() -> None:
    assert evaluate_gate([], [], GatePolicy(), judge_errors=[]).status == "PASS"
