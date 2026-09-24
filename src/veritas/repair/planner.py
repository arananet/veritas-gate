"""RepairPlanner: findings in, a normalized repair contract out.

This is the only path from evaluation to repair. A judge never speaks to a
repair agent: its finding becomes a planned action, and the agent sees the
action. That indirection is what keeps the evaluator and the repairer from
negotiating with each other.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from veritas.models.evaluation import EvaluationResult
from veritas.models.finding import Finding, severity_rank
from veritas.models.repair import (
    ActionType,
    Priority,
    RepairAction,
    RepairPlan,
)
from veritas.repair.permissions import RepairPermissions

if TYPE_CHECKING:  # the ledger is a loop concept; importing it here would cycle
    from veritas.loop.ledger import FindingLedger

JUDGE_ERROR_CATEGORY = "judge-error"

# Categories that describe *missing evidence* rather than a misrepresentation of
# evidence that exists. Resolving one of these means running something new.
EVIDENCE_CATEGORY_HINTS = (
    "experiment",
    "statistics",
    "dataset",
    "sample",
    "baseline",
    "ablation",
)

# Phrases in a recommendation that mean "produce new measurements". An agent
# asked to do this cannot comply honestly, so the action is never autonomous.
NEW_EVIDENCE_PHRASES = (
    "run the experiment",
    "run experiments",
    "re-run",
    "rerun",
    "run multiple",
    "run with",
    "additional runs",
    "more seeds",
    "multiple seeds",
    "new dataset",
    "second dataset",
    "additional dataset",
    "independent dataset",
    "collect data",
    "gather data",
    "measure",
    "benchmark again",
    "conduct",
    "perform an experiment",
    "evaluate on",
)

CATEGORY_ACTION: tuple[tuple[str, ActionType], ...] = (
    ("check/", "code_change"),
    ("test", "test_change"),
    ("reproducibility", "documentation"),
    ("citation", "artifact_edit"),
    ("consistency", "artifact_edit"),
    ("claims/", "artifact_edit"),
    ("dataset", "dataset_change"),
    ("experiment", "experiment"),
    ("statistics", "artifact_edit"),
)


class RepairPlanner:
    """Converts an evaluation into an actionable, permission-checked plan."""

    version = "1"

    def __init__(self, permissions: RepairPermissions | None = None) -> None:
        self.permissions = permissions or RepairPermissions()

    def plan(
        self,
        result: EvaluationResult,
        iteration: int,
        ledger: FindingLedger | None = None,
    ) -> RepairPlan:
        """Build the plan for one iteration."""
        blocking = set(result.gate.blocking_findings)
        evidence_index = _available_evidence(result)
        actions: list[RepairAction] = []
        notes: list[str] = []

        # A judge that failed describes the evaluation, not the artifact. Handing
        # it to a repair agent once asked Codex to fix a provider rate limit by
        # editing a manuscript.
        failed_judges = sorted(
            {
                item.source or item.id
                for item in result.meta_review.findings
                if item.category == JUDGE_ERROR_CATEGORY
            }
        )
        if failed_judges:
            notes.append(
                f"{len(failed_judges)} judge(s) could not complete "
                f"({', '.join(failed_judges)}); their findings are not repairable. "
                "Re-run the evaluation once the cause is resolved."
            )
        repairable = [
            item for item in result.meta_review.findings if item.category != JUDGE_ERROR_CATEGORY
        ]

        # Group findings that describe the same fix at the same location, so the
        # agent gets one instruction per place it has to touch.
        for index, group in enumerate(_group(repairable), start=1):
            primary = max(group, key=lambda item: severity_rank(item.severity))
            action = self._action_for(
                f"ACTION-{index:03d}", group, primary, blocking, evidence_index
            )
            actions.append(action)

        if ledger is not None:
            repeated = [entry.id for entry in ledger.repeated_blockers(2)]
            if repeated:
                notes.append(
                    f"{len(repeated)} blocker(s) have survived a previous repair attempt: "
                    + ", ".join(sorted(repeated))
                )

        human_blocking = [
            action for action in actions if action.priority == "blocking" and not action.autonomous
        ]
        if human_blocking:
            notes.append(
                f"{len(human_blocking)} blocking action(s) require a human decision and "
                "were not dispatched to the repair agent."
            )

        return RepairPlan(
            evaluation_run_id=result.manifest.run_id,
            iteration=iteration,
            actions=actions,
            notes=notes,
        )

    # ------------------------------------------------------------ internals

    def _action_for(
        self,
        action_id: str,
        group: list[Finding],
        primary: Finding,
        blocking: set[str],
        evidence_index: dict[str, list[str]],
    ) -> RepairAction:
        action_type = _action_type_for(primary)
        priority = _priority_for(group, blocking)
        available = _evidence_for(group, evidence_index)
        needs_evidence = _requires_new_evidence(group, available)

        requires_human = needs_evidence
        blocked_reason: str | None = None
        if needs_evidence:
            blocked_reason = (
                "Resolving this finding requires evidence that does not exist in the "
                "artifact. Veritas will not fabricate measurements, results, citations "
                "or datasets to satisfy its own evaluator."
            )
            # An action that needs new evidence is an experiment, whatever the
            # finding's category suggested.
            if action_type in ("artifact_edit", "documentation"):
                action_type = "experiment"
        elif not self.permissions.allows(action_type):
            requires_human = True
            blocked_reason = self.permissions.reason_for(action_type)

        return RepairAction(
            id=action_id,
            finding_ids=[finding.id for finding in group],
            priority=priority,
            action_type=action_type,
            instruction=_instruction_for(primary, group, available, needs_evidence),
            rationale=primary.description.strip(),
            allowed_files=_allowed_files(group),
            available_evidence=available,
            requires_new_evidence=needs_evidence,
            requires_human_approval=requires_human,
            blocked_reason=blocked_reason,
        )


def _group(findings: list[Finding]) -> list[list[Finding]]:
    """Group findings that point at the same location and the same category."""
    groups: dict[tuple[str, str], list[Finding]] = {}
    order: list[tuple[str, str]] = []
    for finding in findings:
        key = (_location_key(finding), finding.category.lower())
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(finding)
    return [groups[key] for key in order]


def _location_key(finding: Finding) -> str:
    return (finding.location or "").strip().lower()


def _priority_for(group: list[Finding], blocking: set[str]) -> Priority:
    if any(finding.id in blocking for finding in group):
        return "blocking"
    worst = max(severity_rank(finding.severity) for finding in group)
    if worst >= severity_rank("major"):
        return "blocking"
    if worst == severity_rank("minor"):
        return "recommended"
    return "optional"


def _action_type_for(finding: Finding) -> ActionType:
    category = finding.category.lower()
    for hint, action_type in CATEGORY_ACTION:
        if hint in category:
            return action_type
    location = (finding.location or "").lower()
    if location.endswith((".py", ".ts", ".js", ".go", ".rs", ".java", ".rb")):
        return "code_change"
    return "artifact_edit"


def _allowed_files(group: list[Finding]) -> list[str]:
    """The files the agent may touch: exactly the ones the findings point at."""
    files: list[str] = []
    for finding in group:
        location = (finding.location or "").strip()
        if not location:
            continue
        path = location.split("#", 1)[0].strip()
        if path and path not in files:
            files.append(path)
    return files


def _available_evidence(result: EvaluationResult) -> dict[str, list[str]]:
    """Evidence already present in the artifact, indexed by finding id."""
    index: dict[str, list[str]] = {}
    for finding in result.meta_review.findings:
        index[finding.id] = list(finding.evidence)
    return index


def _evidence_for(group: list[Finding], index: dict[str, list[str]]) -> list[str]:
    collected: list[str] = []
    for finding in group:
        for item in index.get(finding.id, []):
            if item not in collected:
                collected.append(item)
    return collected


def _requires_new_evidence(group: list[Finding], available: list[str]) -> bool:
    """Decide whether resolving these findings means producing new evidence.

    Conservative on purpose: when in doubt, a human decides. The cost of a
    false positive is one human review; the cost of a false negative is an
    agent inventing a measurement.
    """
    for finding in group:
        haystack = " ".join(
            part.lower()
            for part in (finding.recommendation or "", finding.description, finding.title)
        )
        if any(phrase in haystack for phrase in NEW_EVIDENCE_PHRASES):
            return True
        category = finding.category.lower()
        if any(hint in category for hint in EVIDENCE_CATEGORY_HINTS) and not available:
            # An evidence-shaped finding that cites nothing existing cannot be
            # closed by rewording the artifact.
            return True
        if _asks_for_a_number_that_does_not_exist(finding, available):
            return True
    return False


_COUNT_REQUEST = re.compile(
    r"\b(?:with|use|using|at least|minimum of)\s+(\d+)\s+"
    r"(?:seeds?|runs?|folds?|trials?|datasets?|repetitions?)\b",
    re.IGNORECASE,
)


def _asks_for_a_number_that_does_not_exist(finding: Finding, available: list[str]) -> bool:
    """Catch "report results over 30 seeds" when no such run exists.

    Adding a number the artifact already records is representation work.
    Producing one it does not record is fabrication.
    """
    text = f"{finding.recommendation or ''} {finding.description}"
    match = _COUNT_REQUEST.search(text)
    if match is None:
        return False
    requested = match.group(1)
    evidence_text = " ".join(available).lower()
    return requested not in evidence_text


def _instruction_for(
    primary: Finding,
    group: list[Finding],
    available: list[str],
    needs_evidence: bool,
) -> str:
    """Write the operative instruction the agent receives.

    Note what this is not: it is not the judge's prose handed through. It states
    the problem, what to change, and the standing prohibition on inventing
    evidence.
    """
    parts = [f"Problem: {primary.description.strip()}"]
    if primary.recommendation:
        parts.append(f"Required action: {primary.recommendation.strip()}")
    else:
        parts.append(f"Required action: resolve '{primary.title.strip()}'.")

    if needs_evidence:
        parts.append(
            "This cannot be completed from evidence already present in the artifact. "
            "Do not invent the missing measurements, results, citations or data. "
            "Return this action as requiring human intervention."
        )
    elif available:
        parts.append(
            "Use only the evidence already present in the artifact, listed below. "
            "If a value is not there, do not invent it:\n"
            + "\n".join(f"  - {item}" for item in available)
        )
    else:
        parts.append(
            "Use only what the artifact already contains. Do not invent experimental "
            "results, citations, measurements, datasets or test outcomes."
        )

    if len(group) > 1:
        others = ", ".join(finding.id for finding in group[1:])
        parts.append(f"This action also addresses: {others}.")
    return "\n\n".join(parts)
