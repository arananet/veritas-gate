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
            steps.append(
                Step(
                    f"{len(failed)} judge(s) did not complete; this evaluation is partial. "
                    "Lower `concurrency` if the cause was a rate limit, then re-run.",
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
        steps.append(
            Step(
                f"{len(to_fix)} blocking issue(s) are fixable in the work. Commit first, then:",
                "veritas loop . --workspace worktree" if can_repair else None,
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
