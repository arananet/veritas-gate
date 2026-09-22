"""Markdown report generation."""

from __future__ import annotations

from veritas.models.evaluation import EvaluationResult
from veritas.models.finding import Finding, severity_rank
from veritas.usage import UsageSummary

SEVERITY_LABEL = {
    "critical": "CRITICAL",
    "major": "MAJOR",
    "minor": "MINOR",
    "info": "INFO",
}


def render_markdown(result: EvaluationResult) -> str:
    gate = result.gate
    lines: list[str] = [
        "# Veritas Gate Report",
        "",
        f"**Gate:** `{gate.status}` (exit code {gate.exit_code})",
        "",
        "## Executive summary",
        "",
        result.meta_review.summary or "No summary was produced.",
        "",
        "## Gate result",
        "",
        "| Severity | Count |",
        "| --- | --- |",
        f"| Critical | {gate.critical} |",
        f"| Major | {gate.major} |",
        f"| Minor | {gate.minor} |",
        f"| Info | {gate.info} |",
        "",
    ]
    if gate.reasons:
        lines.append("Policy decisions:")
        lines.append("")
        lines.extend(f"- {reason}" for reason in gate.reasons)
        lines.append("")

    lines.extend(_blocking_section(result))
    lines.extend(_consensus_section(result))
    lines.extend(_disagreement_section(result))
    lines.extend(_coverage_section(result))
    lines.extend(_checks_section(result))
    lines.extend(_findings_section(result))
    lines.extend(_actions_section(result))
    lines.extend(_usage_section(result))
    lines.extend(_metadata_section(result))
    return "\n".join(lines).rstrip() + "\n"


def _blocking_section(result: EvaluationResult) -> list[str]:
    lines = ["## Blocking findings", ""]
    blocking = set(result.gate.blocking_findings)
    findings = [item for item in result.meta_review.findings if item.id in blocking]
    if not findings:
        lines.extend(["None.", ""])
        return lines
    for finding in sorted(findings, key=lambda item: -severity_rank(item.severity)):
        lines.extend(_finding_block(finding))
    return lines


def _finding_block(finding: Finding) -> list[str]:
    lines = [
        f"### [{finding.id}] {finding.title}",
        "",
        f"- **Severity:** {SEVERITY_LABEL[finding.severity]}",
        f"- **Category:** {finding.category}",
        f"- **Location:** {finding.location or 'not specified'}",
        f"- **Confidence:** {finding.confidence:.2f}",
        f"- **Reported by:** {finding.source or 'unknown'}",
        "",
        finding.description,
        "",
    ]
    if finding.evidence:
        lines.append("Evidence:")
        lines.append("")
        lines.extend(f"- {item}" for item in finding.evidence)
        lines.append("")
    else:
        lines.extend(["_No evidence was provided; confidence was reduced accordingly._", ""])
    if finding.recommendation:
        lines.extend([f"**Recommendation:** {finding.recommendation}", ""])
    return lines


def _consensus_section(result: EvaluationResult) -> list[str]:
    lines = [
        "## Judge consensus",
        "",
        "| Finding | Severity | Reported by | Consensus |",
        "| --- | --- | --- | --- |",
    ]
    if not result.meta_review.consolidated:
        return ["## Judge consensus", "", "No findings were reported.", ""]
    for item in result.meta_review.consolidated:
        lines.append(
            f"| {item.finding.id} | {SEVERITY_LABEL[item.finding.severity]} "
            f"| {', '.join(item.reported_by)} | {item.consensus} "
            f"({item.consensus_confidence}) |"
        )
    lines.append("")
    return lines


def _disagreement_section(result: EvaluationResult) -> list[str]:
    lines = ["## Judge disagreements", ""]
    if not result.meta_review.disagreements:
        lines.extend(["No disagreements were recorded.", ""])
        return lines
    for item in result.meta_review.disagreements:
        positions = ", ".join(f"{judge}: {verdict}" for judge, verdict in item.positions.items())
        lines.extend(
            [
                f"- **{item.finding_id}** — {item.title}",
                f"  - Positions: {positions}",
                f"  - {item.note}",
            ]
        )
    lines.append("")
    return lines


def _coverage_section(result: EvaluationResult) -> list[str]:
    coverage = result.coverage
    lines = [
        "## Claim coverage",
        "",
        f"- Claims detected: {coverage.total} ({coverage.major} major)",
        f"- Verified: {coverage.verified}",
        f"- Partially supported: {coverage.partially_supported}",
        f"- Unsupported: {coverage.unsupported}",
        f"- Unverified: {coverage.unverified}",
        f"- Evidence coverage: {coverage.coverage}%",
        "",
        "_Evidence coverage is a diagnostic, not a quality score. "
        "The findings below stand on their own._",
        "",
    ]
    return lines


def _checks_section(result: EvaluationResult) -> list[str]:
    lines = ["## Deterministic checks", ""]
    if not result.check_results:
        lines.extend(["No checks were configured.", ""])
        return lines
    lines.extend(["| Check | Status | Duration | Summary |", "| --- | --- | --- | --- |"])
    for check in result.check_results:
        lines.append(
            f"| {check.check} | {check.status} | {check.duration_seconds:.2f}s | {check.summary} |"
        )
    lines.append("")
    return lines


def _findings_section(result: EvaluationResult) -> list[str]:
    lines = ["## All findings", ""]
    findings = result.meta_review.findings
    if not findings:
        lines.extend(["No findings.", ""])
        return lines
    for finding in findings:
        lines.extend(_finding_block(finding))
    return lines


def _actions_section(result: EvaluationResult) -> list[str]:
    lines = ["## Recommended changes", ""]
    actions = result.meta_review.required_actions
    if not actions:
        lines.extend(["No actions are required.", ""])
        return lines
    for action in actions:
        files = f" (files: {', '.join(action.files)})" if action.files else ""
        lines.append(f"- **{action.finding}** — {action.action}{files}")
    lines.extend(
        [
            "",
            "_Veritas Gate never modifies the artifact it evaluates; these actions are advisory._",
            "",
        ]
    )
    return lines


def _usage_section(result: EvaluationResult) -> list[str]:
    """What the run spent, per model. Cost only when the operator priced it."""
    summary = UsageSummary.model_validate(result.manifest.usage or {})
    if not summary.calls:
        return []

    lines = ["## Model usage", ""]
    header = "| Model | Calls | Input tokens | Output tokens |"
    divider = "| --- | --- | --- | --- |"
    if summary.has_cost:
        header += " Cost |"
        divider += " --- |"
    lines.extend([header, divider])

    for entry in summary.by_model:
        row = (
            f"| {entry.model} | {entry.calls} | {entry.input_tokens:,} | {entry.output_tokens:,} |"
        )
        if summary.has_cost:
            row += f" {_money(entry.cost, summary.currency)} |"
        lines.append(row)

    total = (
        f"| **Total** | {summary.calls} | {summary.input_tokens:,} | {summary.output_tokens:,} |"
    )
    if summary.has_cost:
        total += f" **{_money(summary.cost, summary.currency)}** |"
    lines.extend([total, ""])

    if summary.unpriced_models:
        names = ", ".join(f"`{name}`" for name in summary.unpriced_models)
        lines.extend([f"No price is configured for {names}; the total is incomplete.", ""])
    if summary.calls_without_usage:
        lines.extend(
            [
                f"{summary.calls_without_usage} call(s) reported no usage metadata, "
                "so token counts are a floor rather than a total.",
                "",
            ]
        )
    if summary.has_cost:
        lines.extend(
            [
                "Cost is estimated from the rates configured when the run happened. "
                "It is not a billing record.",
                "",
            ]
        )
    return lines


def _money(value: float | None, currency: str | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.4f} {currency or 'USD'}"


def _metadata_section(result: EvaluationResult) -> list[str]:
    manifest = result.manifest
    lines = [
        "## Evaluation metadata",
        "",
        f"- Run id: `{manifest.run_id}`",
        f"- Veritas version: {manifest.veritas_version}",
        f"- Profile: {manifest.profile} (version {manifest.profile_version})",
        f"- Artifact: {manifest.artifact_id} ({manifest.artifact_type})",
        f"- Artifact commit: {manifest.artifact_commit or 'not a git checkout'}",
        f"- Started: {manifest.started_at.isoformat()}",
        f"- Finished: {manifest.finished_at.isoformat() if manifest.finished_at else 'n/a'}",
        "",
        "### Models",
        "",
    ]
    if manifest.models:
        lines.extend(["| Role | Provider | Model |", "| --- | --- | --- |"])
        for role, model in manifest.models.items():
            lines.append(f"| {role} | {model.get('provider')} | {model.get('model')} |")
    else:
        lines.append("No models were configured.")
    lines.append("")
    if result.stability:
        lines.extend(
            ["### Judge stability", "", "| Judge | Runs | Stability |", "| --- | --- | --- |"]
        )
        for item in result.stability:
            lines.append(f"| {item.judge} | {item.runs} | {item.stability} |")
        lines.append("")
    lines.extend(["### Prompt versions", ""])
    for name, digest in manifest.prompt_versions.items():
        lines.append(f"- `{name}`: `{digest}`")
    lines.append("")
    return lines
