"""What to do next, in a handful of lines.

After every evaluation someone had to read the findings and say: these are fixed
in veritas.yaml, these by a repair run, these are yours to decide, these the
paper already declares. This module says it, from the findings' dispositions.
"""

from __future__ import annotations

from dataclasses import dataclass

from veritas.models.finding import Finding, severity_rank

MAX_LINES = 8
_BLOCKING = severity_rank("major")


@dataclass(slots=True)
class Step:
    text: str
    command: str | None = None


def next_steps(
    findings: list[Finding],
    gate_status: str,
    *,
    can_repair: bool,
    accepted: set[str] | None = None,
    blocking: set[str] | None = None,
) -> list[Step]:
    """Ordered steps: configuration first, then repairs, then decisions.

    A finding the operator accepted as a known risk asks nothing more of them,
    so it is left out. When the gate says which findings block, that is what
    counts as blocking -- not a severity guess, which once reported four
    blocking issues beside a gate that listed one.
    """
    if gate_status == "PASS":
        return [Step("Nothing blocks this work. Commit it, and deposit the version you cite.")]
    if gate_status == "PASS_WITH_WARNINGS":
        # The gate passed: calling its warnings "blocking" contradicted the
        # verdict printed just above.
        accepted = accepted or set()
        warned = [f for f in findings if f.id in (blocking or set()) and f.id not in accepted]
        passed = [Step("Nothing blocks this work. Commit it, and deposit the version you cite.")]
        if warned:
            passed.append(
                Step(
                    f"{len(warned)} warning(s) worth a look, none blocking; "
                    "fix any you agree with, or leave them.",
                    "veritas findings .",
                )
            )
        return passed

    accepted = accepted or set()
    live = [f for f in findings if f.id not in accepted]
    config = [f for f in live if f.disposition == "configuration"]
    if blocking is not None:
        to_fix = [f for f in live if f.disposition == "artifact" and f.id in blocking]
    else:
        to_fix = [
            f
            for f in live
            if f.disposition == "artifact" and severity_rank(f.severity) >= _BLOCKING
        ]
    decisions = [f for f in live if f.disposition == "decision"]
    declared = [f for f in live if f.disposition == "declared"]
    missing_files = [f for f in live if f.category == "check/archival-files"]

    steps: list[Step] = []
    if missing_files:
        steps.append(
            Step(
                "Citation or licensing files are missing. Create them from your declared metadata:",
                "veritas scaffold .",
            )
        )
    if config:
        failed = [f for f in config if f.category == "judge-error"]
        unsupplied = [f for f in config if f.category.endswith("/unsupplied")]
        other = [f for f in config if f not in failed and f not in unsupplied]
        if failed:
            too_large = any(_is_too_large(f) for f in failed)
            advice = (
                "Each request is larger than the provider allows per minute, so no wait "
                "will help: narrow `artifact.paths`, or scope judges with `judge_paths`."
                if too_large
                else "Lower `concurrency` if the cause was a rate limit, then re-run."
            )
            steps.append(
                Step(
                    f"{len(failed)} judge(s) did not complete; this evaluation is partial. "
                    + advice,
                    "veritas evaluate .",
                )
            )
        if unsupplied:
            steps.append(
                Step(
                    f"{len(unsupplied)} cited path(s) are outside artifact.paths. Add them, "
                    "or accept the exclusion if it is deliberate."
                )
            )
        if other:
            steps.append(Step(f"{len(other)} configuration issue(s): {_titles(other)}."))
    if to_fix:
        if can_repair:
            steps.append(
                Step(
                    f"{len(to_fix)} blocking issue(s) are fixable in the work. Commit first, then:",
                    "veritas loop . --workspace worktree",
                )
            )
        else:
            steps.append(
                Step(
                    f"{len(to_fix)} blocking issue(s) remain in the work: "
                    f"{_titles(to_fix)}. Fix them by hand, then run `veritas evaluate .`."
                )
            )
    if decisions:
        steps.append(Step(f"{len(decisions)} need your decision: {_titles(decisions)}."))
    if declared:
        steps.append(
            Step(f"{len(declared)} declared limitation(s), weighed as minor. Nothing to do.")
        )
    if not steps:
        steps.append(Step("No blocking issue has a clear owner; read the blocking findings."))
    return _bounded(steps)


def _titles(findings: list[Finding], limit: int = 3) -> str:
    names = [f.title.rstrip(".") for f in findings[:limit]]
    more = len(findings) - limit
    return "; ".join(names) + (f"; and {more} more" if more > 0 else "")


def _bounded(steps: list[Step]) -> list[Step]:
    lines = 0
    kept: list[Step] = []
    for step in steps:
        cost = 1 + (1 if step.command else 0)
        if lines + cost > MAX_LINES:
            break
        kept.append(step)
        lines += cost
    return kept


def _is_too_large(finding: Finding) -> bool:
    text = finding.description.lower()
    return "request too large" in text or "must be reduced" in text
