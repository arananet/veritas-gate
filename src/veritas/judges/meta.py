"""MetaJudge: consolidate independent judge results without losing anything.

The merge itself is deterministic Python. A model may be consulted afterwards
for the narrative summary and required actions, but it can never delete,
downgrade or average away a finding — severity is always the maximum reported
by any judge, and every disagreement is recorded rather than resolved silently.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from veritas.judges.llm import _model_id
from veritas.models.evaluation import (
    CheckResult,
    ConsolidatedFinding,
    Disagreement,
    JudgeResult,
    MetaReview,
    RequiredAction,
)
from veritas.models.finding import Finding, Severity, max_severity, severity_rank
from veritas.providers.base import ModelProvider, ProviderError
from veritas.security import UNTRUSTED_PREAMBLE

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "has",
    "in",
    "is",
    "it",
    "its",
    "no",
    "not",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
    "without",
}
_WORD = re.compile(r"[a-z0-9]+")
Consensus = Literal["single-source", "confirmed", "disputed"]
ConsensusConfidence = Literal["low", "medium", "high"]

SIMILARITY_THRESHOLD = 0.55
EVIDENCE_THRESHOLD = 0.25


class MetaAction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    finding: str
    action: str
    files: list[str] = Field(default_factory=list)


class MetaResponse(BaseModel):
    """What the meta model may contribute. Findings are not among them."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    required_actions: list[MetaAction] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class MetaJudge:
    """Deterministic consolidation, with optional narrative assistance."""

    name = "meta"
    version = "1"

    def __init__(self, provider: ModelProvider | None = None) -> None:
        self.provider = provider

    async def review(
        self,
        judge_results: Iterable[JudgeResult],
        check_results: Iterable[CheckResult],
    ) -> MetaReview:
        judge_results = list(judge_results)
        check_results = list(check_results)
        # Judge-error findings are kept. Discarding them made a run where every
        # judge failed look like a run with nothing to report.
        all_findings = [finding for result in judge_results for finding in result.findings]
        all_findings.extend(finding for result in check_results for finding in result.findings)

        consolidated = consolidate(all_findings)
        disagreements = detect_disagreements(consolidated, judge_results)
        review = MetaReview(
            consolidated=consolidated,
            disagreements=disagreements,
            unresolved=[item.finding_id for item in disagreements],
            required_actions=default_actions(consolidated),
            summary=deterministic_summary(consolidated, judge_results, check_results),
            metadata={
                "judges": [result.judge for result in judge_results],
                "checks": [result.check for result in check_results],
                "merge": "deterministic",
            },
        )
        if self.provider is not None:
            review = await self._augment(review)
        return review

    async def _augment(self, review: MetaReview) -> MetaReview:
        assert self.provider is not None
        system = (
            UNTRUSTED_PREAMBLE
            + "\n\nYou are consolidating the results of independent evaluators.\n"
            "You may write a summary and propose concrete required actions.\n"
            "You must NOT remove, merge away, downgrade or average any finding.\n"
            "Reference findings only by the ids given to you.\n"
            "Return only the required structured schema."
        )
        lines = []
        for item in review.consolidated:
            finding = item.finding
            lines.append(
                f"{finding.id} [{finding.severity}] {finding.title} "
                f"(reported by {', '.join(item.reported_by)}; consensus {item.consensus}) "
                f"location={finding.location or 'n/a'} :: {finding.description}"
            )
        user = (
            "Consolidated findings:\n"
            + ("\n".join(lines) or "(none)")
            + "\n\nDisagreements:\n"
            + (
                "\n".join(f"{item.finding_id}: {item.positions}" for item in review.disagreements)
                or "(none)"
            )
            + "\n\nWrite the executive summary and the required actions."
        )
        try:
            response = await self.provider.generate_structured(system, user, MetaResponse)
        except ProviderError as exc:
            review.metadata["meta_model_error"] = str(exc)
            return review

        payload = response.value
        assert isinstance(payload, MetaResponse)
        known = {item.finding.id for item in review.consolidated}
        actions = [
            RequiredAction(finding=action.finding, action=action.action, files=action.files)
            for action in payload.required_actions
            if action.finding in known and action.action.strip()
        ]
        if actions:
            review.required_actions = _merge_actions(review.required_actions, actions)
        if payload.summary.strip():
            review.summary = payload.summary.strip()
        review.unresolved = sorted(
            {*review.unresolved, *[item for item in payload.unresolved if item in known]}
        )
        review.metadata["meta_model_usage"] = response.usage
        review.metadata["meta_model"] = _model_id(self.provider)
        review.metadata["meta_provider"] = getattr(self.provider, "name", None)
        return review


def consolidate(findings: list[Finding]) -> list[ConsolidatedFinding]:
    """Group duplicate findings, keeping the worst severity reported by anyone."""
    groups: list[list[Finding]] = []
    for finding in findings:
        for group in groups:
            if _is_duplicate(finding, group[0]):
                group.append(finding)
                break
        else:
            groups.append([finding])

    consolidated: list[ConsolidatedFinding] = []
    for group in groups:
        primary = max(group, key=lambda item: (severity_rank(item.severity), item.confidence))
        worst = max_severity([item.severity for item in group])
        sources = []
        for item in group:
            source = item.source or "unknown"
            if source not in sources:
                sources.append(source)
        merged_evidence: list[str] = []
        for item in group:
            for line in item.evidence:
                if line not in merged_evidence:
                    merged_evidence.append(line)
        finding = primary.model_copy(
            update={
                "severity": worst,
                "evidence": merged_evidence,
                "confidence": _group_confidence(group, sources),
            }
        )
        severities: dict[str, Severity] = {
            item.source or "unknown": item.severity for item in group
        }
        consensus, confidence = _consensus(sources, severities)
        consolidated.append(
            ConsolidatedFinding(
                finding=finding,
                reported_by=sources,
                severities=severities,
                consensus=consensus,
                consensus_confidence=confidence,
                duplicate_ids=[item.id for item in group if item.id != primary.id],
            )
        )
    consolidated.sort(key=lambda item: (-severity_rank(item.finding.severity), item.finding.id))
    return consolidated


def _group_confidence(group: list[Finding], sources: list[str]) -> float:
    base = max(item.confidence for item in group)
    if len(sources) > 1:
        base = min(1.0, base + 0.1 * (len(sources) - 1))
    return round(base, 3)


def _consensus(
    sources: list[str], severities: Mapping[str, str]
) -> tuple[Consensus, ConsensusConfidence]:
    if len(sources) < 2:
        return "single-source", "low"
    distinct = set(severities.values())
    if len(distinct) == 1:
        return "confirmed", "high" if len(sources) > 2 else "medium"
    return "disputed", "medium"


def _is_duplicate(left: Finding, right: Finding) -> bool:
    """Decide whether two judges are describing the same issue.

    Judges paraphrase, so title overlap alone misses real duplicates. Two
    findings at the same location that cite overlapping evidence are treated as
    one issue even when worded differently — the merge keeps the worst severity,
    so a wrong merge cannot hide a concern.
    """
    if left.source and right.source and left.source == right.source:
        # One judge listing two findings means it considered them distinct.
        return False
    same_location = bool(
        left.location and right.location and left.location.strip() == right.location.strip()
    )
    if same_location:
        if _similarity(left.title, right.title) >= 0.3:
            return True
        if _similarity(_evidence_text(left), _evidence_text(right)) >= EVIDENCE_THRESHOLD:
            return True
    return (
        _similarity(f"{left.title} {left.description}", f"{right.title} {right.description}")
        >= SIMILARITY_THRESHOLD
    )


def _evidence_text(finding: Finding) -> str:
    return " ".join(finding.evidence)


def _tokens(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}


def _similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def detect_disagreements(
    consolidated: list[ConsolidatedFinding], judge_results: list[JudgeResult]
) -> list[Disagreement]:
    """Record conflicts. A concern raised by one judge and missed by another stays visible."""
    passing = [
        result.judge for result in judge_results if result.status == "pass" and not result.findings
    ]
    disagreements: list[Disagreement] = []
    for item in consolidated:
        # Severity strings, plus the literal "pass" for judges that saw nothing.
        positions: dict[str, str] = dict(item.severities)
        note = ""
        if item.consensus == "disputed":
            note = "Judges reported different severities for the same issue."
        elif (
            passing
            and severity_rank(item.finding.severity) >= severity_rank("major")
            and item.consensus == "single-source"
        ):
            for judge in passing:
                positions[judge] = "pass"
            note = (
                "Raised by one judge while others reported no issue. "
                "The concern is retained, not averaged away."
            )
        if note:
            disagreements.append(
                Disagreement(
                    finding_id=item.finding.id,
                    title=item.finding.title,
                    positions=positions,
                    note=note,
                )
            )
    return disagreements


def default_actions(consolidated: list[ConsolidatedFinding]) -> list[RequiredAction]:
    """Derive an action per blocking-grade finding from its own recommendation."""
    actions: list[RequiredAction] = []
    for item in consolidated:
        finding = item.finding
        if severity_rank(finding.severity) < severity_rank("major"):
            continue
        action = finding.recommendation or f"Resolve: {finding.title}"
        files = [finding.location] if finding.location else []
        actions.append(RequiredAction(finding=finding.id, action=action.strip(), files=files))
    return actions


def _merge_actions(base: list[RequiredAction], extra: list[RequiredAction]) -> list[RequiredAction]:
    merged = {action.finding: action for action in base}
    for action in extra:
        merged[action.finding] = action
    return [merged[key] for key in sorted(merged)]


def deterministic_summary(
    consolidated: list[ConsolidatedFinding],
    judge_results: list[JudgeResult],
    check_results: list[CheckResult],
) -> str:
    counts: dict[str, int] = {"critical": 0, "major": 0, "minor": 0, "info": 0}
    for item in consolidated:
        counts[item.finding.severity] += 1
    failed = [result.check for result in check_results if not result.passed]
    parts = [
        f"{len(judge_results)} judges and {len(check_results)} checks produced "
        f"{len(consolidated)} consolidated findings "
        f"({counts['critical']} critical, {counts['major']} major, "
        f"{counts['minor']} minor, {counts['info']} info)."
    ]
    if failed:
        parts.append(f"Failing checks: {', '.join(failed)}.")
    return " ".join(parts)
