"""The finding ledger.

Judges assign finding ids per run, so the same issue gets a different id on
every evaluation. The ledger gives each *logical* issue a stable identity across
iterations, which is what makes "was this blocker actually resolved?" answerable
instead of a guess based on counting.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from veritas.models.evaluation import EvaluationResult
from veritas.models.finding import Finding, issue_key, severity_rank

# issue_key lives with the finding model now; re-exported here because the
# ledger is where its cross-iteration meaning is defined.
__all__ = ["FindingLedger", "issue_key"]
from veritas.models.loop import LedgerEntry, LedgerStatus

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
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
}
MATCH_THRESHOLD = 0.5


def _tokens(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS}


def _similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class FindingLedger:
    """Tracks issue identity and status across the iterations of one loop."""

    def __init__(self, entries: list[LedgerEntry] | None = None) -> None:
        self.entries: dict[str, LedgerEntry] = {entry.id: entry for entry in entries or []}

    # ------------------------------------------------------------- lookups

    def get(self, entry_id: str) -> LedgerEntry | None:
        return self.entries.get(entry_id)

    def open_blockers(self) -> list[LedgerEntry]:
        return [
            entry
            for entry in self.entries.values()
            if entry.blocking and entry.status in ("OPEN", "UNCHANGED", "REGRESSED", "IMPROVED")
        ]

    def human_review(self) -> list[LedgerEntry]:
        return [entry for entry in self.entries.values() if entry.status == "HUMAN_REVIEW"]

    def repeated_blockers(self, limit: int) -> list[LedgerEntry]:
        """Blockers that survived ``limit`` or more iterations unresolved."""
        return [entry for entry in self.open_blockers() if len(entry.seen_in_iterations) >= limit]

    def as_list(self) -> list[LedgerEntry]:
        return sorted(
            self.entries.values(),
            key=lambda entry: (-severity_rank(entry.severity), entry.id),
        )

    # ------------------------------------------------------------- updates

    def match(self, finding: Finding) -> LedgerEntry | None:
        """Find the existing entry this finding belongs to, if any."""
        key = issue_key(finding)
        if key in self.entries:
            return self.entries[key]
        best: tuple[float, LedgerEntry] | None = None
        for entry in self.entries.values():
            if entry.category.lower() != finding.category.lower():
                continue
            if (entry.location or "") != (finding.location or ""):
                continue
            score = _similarity(entry.title, finding.title)
            if score >= MATCH_THRESHOLD and (best is None or score > best[0]):
                best = (score, entry)
        return best[1] if best else None

    def record(self, result: EvaluationResult, iteration: int) -> None:
        """Fold one evaluation into the ledger.

        Issues present in this evaluation are updated; issues absent from it
        that were previously open are marked RESOLVED. Statuses come from what
        the evaluator found, never from a repair agent's claim of success.
        """
        blocking = set(result.gate.blocking_findings)
        seen: set[str] = set()

        for finding in result.meta_review.findings:
            entry = self.match(finding)
            is_blocking = finding.id in blocking
            if entry is None:
                entry = LedgerEntry(
                    id=issue_key(finding),
                    title=finding.title,
                    category=finding.category,
                    severity=finding.severity,
                    location=finding.location,
                    status="OPEN",
                    first_seen_iteration=iteration,
                    last_seen_iteration=iteration,
                    seen_in_iterations=[iteration],
                    severity_history=[finding.severity],
                    finding_ids=[finding.id],
                    blocking=is_blocking,
                )
                self.entries[entry.id] = entry
                seen.add(entry.id)
                continue

            previous = entry.severity
            entry.last_seen_iteration = iteration
            if iteration not in entry.seen_in_iterations:
                entry.seen_in_iterations.append(iteration)
            entry.severity_history.append(finding.severity)
            if finding.id not in entry.finding_ids:
                entry.finding_ids.append(finding.id)
            entry.severity = finding.severity
            entry.blocking = is_blocking
            entry.status = _status_for(previous, finding.severity, entry.status)
            seen.add(entry.id)

        for entry in self.entries.values():
            if entry.id in seen:
                continue
            # An issue the evaluator no longer reports is resolved — including one
            # that was awaiting a human, since a resumed loop is exactly the case
            # where the human did the work outside the loop.
            if entry.status in ("OPEN", "UNCHANGED", "IMPROVED", "REGRESSED", "HUMAN_REVIEW"):
                previous_status = entry.status
                entry.status = "RESOLVED"
                entry.blocking = False
                note = f"not reported in iteration {iteration}"
                if previous_status == "HUMAN_REVIEW":
                    note += " (resolved outside the loop)"
                entry.notes.append(note)

    def mark_human_review(self, entry_ids: list[str], note: str) -> None:
        for entry_id in entry_ids:
            entry = self.entries.get(entry_id)
            if entry is not None and entry.status != "RESOLVED":
                entry.status = "HUMAN_REVIEW"
                entry.notes.append(note)

    def entries_for_findings(self, findings: list[Finding]) -> dict[str, str]:
        """Map finding id to ledger id for the findings of one evaluation."""
        mapping: dict[str, str] = {}
        for finding in findings:
            entry = self.match(finding)
            mapping[finding.id] = entry.id if entry else issue_key(finding)
        return mapping

    # ---------------------------------------------------------- persistence

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = [entry.model_dump(mode="json") for entry in self.as_list()]
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> FindingLedger:
        if not path.is_file():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls([LedgerEntry.model_validate(item) for item in payload])


def _status_for(previous: str, current: str, existing: LedgerStatus) -> LedgerStatus:
    if existing == "HUMAN_REVIEW":
        return existing
    before, after = severity_rank(previous), severity_rank(current)
    if after < before:
        return "IMPROVED"
    if after > before:
        return "REGRESSED"
    return "UNCHANGED"
