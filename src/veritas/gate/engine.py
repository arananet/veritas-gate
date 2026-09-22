"""The gate engine.

The pass/fail decision is deterministic Python. No model is consulted here, so
the same findings and the same policy always produce the same status and the
same process exit code.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from veritas.config import GatePolicy
from veritas.gate.policy import severity_counts
from veritas.models.evaluation import CheckResult, ClaimCoverage, GateResult
from veritas.models.finding import Finding, issue_key, severity_rank


def evaluate_gate(
    findings: list[Finding],
    check_results: list[CheckResult],
    policy: GatePolicy,
    coverage: ClaimCoverage | None = None,
    judge_errors: list[str] | None = None,
    today: date | None = None,
) -> GateResult:
    """Apply ``policy`` to the consolidated findings and check results."""
    judge_errors = list(judge_errors or [])

    # Two counts, deliberately. `counts` is what the run reports and what the
    # user sees: it includes accepted risks, because accepting a finding must
    # never make it disappear. `findings` below is what the policy is applied
    # to, with accepted risks removed, because that is what accepting means.
    counts = severity_counts(findings)
    accepted_ids, accepted, stale, accepted_findings = _partition_accepted(findings, policy, today)
    findings = [item for item in findings if item.id not in accepted_ids]
    blocking_counts = severity_counts(findings)
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
        if blocking_counts[severity] > limit:
            offenders = [item.id for item in findings if item.severity == severity]
            blocking.extend(offenders)
            reasons.append(
                f"{blocking_counts[severity]} {severity} finding(s) exceed the limit of {limit}."
            )
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

    if accepted:
        reasons.append(
            f"{len(accepted)} rule(s) accepted {len(accepted_findings)} finding(s) as "
            "known risk, so they do not block: " + ", ".join(accepted)
        )
    if stale:
        reasons.append(
            f"{len(stale)} accepted risk(s) no longer match any finding or have expired: "
            + ", ".join(stale)
        )

    if fail:
        status = "FAIL"
    elif revise:
        status = "REVISE"
    elif blocking_counts["minor"] and policy.warn_on_minor:
        status = "PASS_WITH_WARNINGS"
        reasons.append(f"{blocking_counts['minor']} minor finding(s) present.")
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
        accepted_risks=accepted,
        accepted_findings=accepted_findings,
        stale_accepted_risks=stale,
        reasons=reasons,
    )


def _partition_accepted(
    findings: list[Finding],
    policy: GatePolicy,
    today: date | None,
) -> tuple[set[str], list[str], list[str], list[str]]:
    """Split the accepted risks into those in force and those gone stale.

    Returns the per-run ids to exclude from blocking, the rules actually
    applied, the rules matching nothing (or expired), and the findings each
    rule covered. That last list matters: a rule written by category can cover
    many findings at once, and a blanket exemption nobody can see the reach of
    is how a gate stops meaning anything.
    """
    if not policy.accepted_risks:
        return set(), [], [], []

    now = today or datetime.now(UTC).date()

    excluded: set[str] = set()
    accepted: list[str] = []
    stale: list[str] = []
    covered: list[str] = []
    for risk in policy.accepted_risks:
        matched = [
            finding
            for finding in findings
            if risk.matches(finding.category, finding.location, issue_key(finding))
        ]
        if not matched or not risk.active_on(now):
            stale.append(risk.label)
            continue
        excluded.update(finding.id for finding in matched)
        accepted.append(risk.label)
        covered.extend(finding.id for finding in matched)
    return excluded, sorted(accepted), sorted(stale), sorted(dict.fromkeys(covered))
