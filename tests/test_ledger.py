"""Finding ledger: stable issue identity across iterations."""

from __future__ import annotations

from pathlib import Path

from veritas.loop.ledger import FindingLedger, issue_key
from veritas.models import Finding
from veritas.models.evaluation import (
    ConsolidatedFinding,
    EvaluationResult,
    GateResult,
    MetaReview,
    RunManifest,
)


def finding(fid: str, title: str, severity: str = "major", **kwargs) -> Finding:
    return Finding(
        id=fid,
        title=title,
        severity=severity,  # type: ignore[arg-type]
        category=kwargs.pop("category", "statistics"),
        description=kwargs.pop("description", "d"),
        location=kwargs.pop("location", "paper/main.md#results"),
        **kwargs,
    )


def evaluation(findings: list[Finding], blocking: list[str] | None = None) -> EvaluationResult:
    from datetime import UTC, datetime

    gate = GateResult(
        status="REVISE" if findings else "PASS",
        blocking_findings=blocking if blocking is not None else [item.id for item in findings],
    )
    return EvaluationResult(
        manifest=RunManifest(
            run_id="r",
            started_at=datetime.now(UTC),
            veritas_version="0.1.0",
            profile="p",
            profile_version="1",
            artifact_id="a",
            artifact_type="document",
        ),
        meta_review=MetaReview(
            consolidated=[ConsolidatedFinding(finding=item) for item in findings]
        ),
        gate=gate,
    )


def test_issue_key_is_stable_across_reworded_titles() -> None:
    left = finding("A-1", "Missing variance in reported results")
    right = finding("Z-9", "Missing variance in reported results")
    assert issue_key(left) == issue_key(right)


def test_issue_key_differs_for_different_locations() -> None:
    left = finding("A-1", "Missing variance", location="paper/a.md")
    right = finding("A-1", "Missing variance", location="paper/b.md")
    assert issue_key(left) != issue_key(right)


def test_a_finding_absent_from_the_next_evaluation_is_resolved() -> None:
    ledger = FindingLedger()
    ledger.record(evaluation([finding("A-1", "Missing variance")]), 1)
    entry_id = next(iter(ledger.entries))
    assert ledger.entries[entry_id].status == "OPEN"

    ledger.record(evaluation([]), 2)
    assert ledger.entries[entry_id].status == "RESOLVED"
    assert not ledger.entries[entry_id].blocking


def test_a_downgraded_finding_is_improved_not_resolved() -> None:
    ledger = FindingLedger()
    ledger.record(evaluation([finding("A-1", "Missing variance", "critical")]), 1)
    entry_id = next(iter(ledger.entries))
    ledger.record(evaluation([finding("B-1", "Missing variance", "minor")]), 2)
    assert ledger.entries[entry_id].status == "IMPROVED"
    assert ledger.entries[entry_id].severity == "minor"


def test_an_escalated_finding_is_regressed() -> None:
    ledger = FindingLedger()
    ledger.record(evaluation([finding("A-1", "Missing variance", "minor")]), 1)
    entry_id = next(iter(ledger.entries))
    ledger.record(evaluation([finding("B-1", "Missing variance", "critical")]), 2)
    assert ledger.entries[entry_id].status == "REGRESSED"


def test_a_reworded_finding_matches_the_same_entry() -> None:
    ledger = FindingLedger()
    ledger.record(evaluation([finding("A-1", "Reported results are missing variance")]), 1)
    ledger.record(evaluation([finding("B-1", "Results are missing reported variance")]), 2)
    assert len(ledger.entries) == 1
    entry = next(iter(ledger.entries.values()))
    assert entry.seen_in_iterations == [1, 2]


def test_repeated_blockers_are_reported() -> None:
    ledger = FindingLedger()
    for iteration in (1, 2):
        ledger.record(evaluation([finding("A-1", "Missing variance")]), iteration)
    assert len(ledger.repeated_blockers(2)) == 1
    assert len(ledger.repeated_blockers(3)) == 0


def test_human_review_status_survives_later_iterations() -> None:
    ledger = FindingLedger()
    ledger.record(evaluation([finding("A-1", "Missing variance")]), 1)
    entry_id = next(iter(ledger.entries))
    ledger.mark_human_review([entry_id], "needs a new experiment")
    ledger.record(evaluation([finding("B-1", "Missing variance")]), 2)
    assert ledger.entries[entry_id].status == "HUMAN_REVIEW"


def test_ledger_round_trips_to_disk(tmp_path: Path) -> None:
    ledger = FindingLedger()
    ledger.record(evaluation([finding("A-1", "Missing variance")]), 1)
    path = tmp_path / "ledger.json"
    ledger.save(path)

    reloaded = FindingLedger.load(path)
    assert set(reloaded.entries) == set(ledger.entries)
    # A resumed loop continues the same issue, it does not open a second one.
    reloaded.record(evaluation([finding("C-1", "Missing variance")]), 2)
    assert len(reloaded.entries) == 1


def test_loading_a_missing_ledger_gives_an_empty_one(tmp_path: Path) -> None:
    assert FindingLedger.load(tmp_path / "absent.json").entries == {}
