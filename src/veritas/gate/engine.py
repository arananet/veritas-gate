"""The gate engine.

The pass/fail decision is deterministic Python. No model is consulted here, so
the same findings and the same policy always produce the same status and the
same process exit code.
"""

from __future__ import annotations

from veritas.config import GatePolicy
from veritas.gate.policy import severity_counts
from veritas.models.evaluation import CheckResult, ClaimCoverage, GateResult
from veritas.models.finding import Finding, severity_rank


def evaluate_gate(
    findings: list[Finding],
    check_results: list[CheckResult],
    policy: GatePolicy,
    coverage: ClaimCoverage | None = None,
    judge_errors: list[str] | None = None,
) -> GateResult:
    """Apply ``policy`` to the consolidated findings and check results."""
    counts = severity_counts(findings)
    judge_errors = list(judge_errors or [])
    blocking: list[str] = []
    reasons: list[str] = []
    fail = False
    revise = False

    # 1. Severities listed in fail_on are absolute blockers.
    for severity in policy.fail_on:
        matched = [item for item in findings if item.severity == severity]
        if matched:
            fail = True
            blocking.extend(item.id for item in matched)
            reasons.append(f"{len(matched)} {severity} finding(s) present (fail_on: {severity}).")

    # 2. Count limits.
    for severity, limit in (
        ("critical", policy.max_critical),
        ("major", policy.max_major),
        ("minor", policy.max_minor),
    ):
        if limit is None or severity in policy.fail_on:
            continue
        if counts[severity] > limit:
            offenders = [item.id for item in findings if item.severity == severity]
            blocking.extend(offenders)
            reasons.append(f"{counts[severity]} {severity} finding(s) exceed the limit of {limit}.")
            if severity_rank(severity) >= severity_rank("major"):
                revise = True

    # 3. Severities explicitly configured to force a revision.
    for severity in policy.revise_on:
        matched = [item for item in findings if item.severity == severity]
        if matched:
            revise = True
            blocking.extend(item.id for item in matched)
            reasons.append(f"{len(matched)} {severity} finding(s) present (revise_on).")

    # 4. Required checks must have run and passed.
    results = {result.check: result for result in check_results}
    failed_checks: list[str] = []
    for name in policy.require_checks:
        result = results.get(name)
        if result is None:
            failed_checks.append(name)
            reasons.append(f"Required check '{name}' did not run.")
            fail = True
        elif not result.passed:
            failed_checks.append(name)
            reasons.append(f"Required check '{name}' failed.")
            fail = True
            blocking.extend(item.id for item in result.findings)

    # 5. Optional evidence-coverage floor.
    if (
        policy.min_evidence_coverage is not None
        and coverage is not None
        and coverage.major
        and coverage.coverage < policy.min_evidence_coverage
    ):
        revise = True
        reasons.append(
            f"Evidence coverage {coverage.coverage}% is below the required "
            f"{policy.min_evidence_coverage}%."
        )

    # 6. A judge that could not run did not approve anything. Without this, a run
    # where every judge errored reports zero findings and passes the gate.
    if judge_errors and policy.fail_on_judge_error:
        fail = True
        reasons.append(
            f"{len(judge_errors)} judge(s) failed to complete "
            f"({', '.join(judge_errors)}); the artifact was not fully evaluated."
        )

    if fail:
        status = "FAIL"
    elif revise:
        status = "REVISE"
    elif counts["minor"] and policy.warn_on_minor:
        status = "PASS_WITH_WARNINGS"
        reasons.append(f"{counts['minor']} minor finding(s) present.")
    else:
        status = "PASS"
        reasons.append("No blocking findings under the configured policy.")

    return GateResult(
        status=status,  # type: ignore[arg-type]
        critical=counts["critical"],
        major=counts["major"],
        minor=counts["minor"],
        info=counts["info"],
        blocking_findings=sorted(dict.fromkeys(blocking)),
        failed_checks=failed_checks,
        judge_errors=judge_errors,
        reasons=reasons,
    )
