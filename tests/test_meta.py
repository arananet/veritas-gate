"""MetaJudge consolidation: nothing important may be averaged away."""

from __future__ import annotations

from veritas.judges.meta import MetaJudge, consolidate, detect_disagreements
from veritas.models import CheckResult, Finding, JudgeResult


def finding(fid: str, source: str, severity: str, title: str, description: str = "") -> Finding:
    return Finding(
        id=fid,
        title=title,
        severity=severity,  # type: ignore[arg-type]
        category="test",
        description=description or title,
        evidence=["e"],
        source=source,
        confidence=0.7,
    )


def test_duplicate_findings_from_different_judges_are_grouped() -> None:
    items = [
        finding("A-1", "methodology", "major", "Missing variance in reported results"),
        finding("B-1", "statistics", "major", "Reported results are missing variance"),
    ]
    consolidated = consolidate(items)
    assert len(consolidated) == 1
    assert set(consolidated[0].reported_by) == {"methodology", "statistics"}
    assert consolidated[0].consensus == "confirmed"


def test_same_judge_findings_are_never_merged() -> None:
    items = [
        finding("A-1", "methodology", "minor", "Missing variance in reported results"),
        finding("A-2", "methodology", "minor", "Missing variance in reported results"),
    ]
    assert len(consolidate(items)) == 2


def test_group_takes_the_worst_severity_not_the_average() -> None:
    items = [
        finding("A-1", "judge-a", "critical", "Experimental result is unsupported"),
        finding("B-1", "judge-b", "minor", "Experimental result is unsupported"),
    ]
    consolidated = consolidate(items)
    assert len(consolidated) == 1
    assert consolidated[0].finding.severity == "critical"
    assert consolidated[0].consensus == "disputed"


def test_evidence_from_every_reporter_is_preserved() -> None:
    left = finding("A-1", "judge-a", "major", "Unsupported claim in section 5")
    right = finding("B-1", "judge-b", "major", "Claim in section 5 is unsupported")
    right = right.model_copy(update={"evidence": ["second reviewer evidence"]})
    consolidated = consolidate([left, right])
    assert consolidated[0].finding.evidence == ["e", "second reviewer evidence"]


def test_consensus_raises_confidence() -> None:
    single = consolidate([finding("A-1", "judge-a", "major", "Only one reviewer saw this")])
    both = consolidate(
        [
            finding("A-1", "judge-a", "major", "Both reviewers saw this problem"),
            finding("B-1", "judge-b", "major", "Both reviewers saw this problem"),
        ]
    )
    assert both[0].finding.confidence > single[0].finding.confidence


def test_a_lone_major_against_passing_judges_is_recorded_as_a_disagreement() -> None:
    consolidated = consolidate([finding("A-1", "judge-a", "major", "Result is unsupported")])
    results = [
        JudgeResult(judge="judge-a", status="fail", findings=[consolidated[0].finding]),
        JudgeResult(judge="judge-b", status="pass"),
    ]
    disagreements = detect_disagreements(consolidated, results)
    assert len(disagreements) == 1
    assert disagreements[0].positions["judge-b"] == "pass"


async def test_meta_review_keeps_the_concern_when_one_judge_passes() -> None:
    """Judge A: CRITICAL. Judge B: PASS. Judge C: MAJOR. The concern survives."""
    results = [
        JudgeResult(
            judge="judge-a",
            status="fail",
            findings=[finding("A-1", "judge-a", "critical", "Experimental result unsupported")],
        ),
        JudgeResult(judge="judge-b", status="pass"),
        JudgeResult(
            judge="judge-c",
            status="fail",
            findings=[
                finding("C-1", "judge-c", "minor", "Experimental result is unsupported by evidence")
            ],
        ),
    ]
    review = await MetaJudge().review(results, [])
    severities = [item.finding.severity for item in review.consolidated]
    assert "critical" in severities
    assert review.disagreements
    assert review.required_actions


async def test_check_findings_join_the_same_consolidation() -> None:
    check = CheckResult(
        check="tests",
        status="fail",
        findings=[finding("CHECK-TESTS-001", "check:tests", "major", "Test suite failed")],
    )
    review = await MetaJudge().review([], [check])
    assert [item.finding.id for item in review.consolidated] == ["CHECK-TESTS-001"]


def test_paraphrased_findings_at_one_location_merge_via_shared_evidence() -> None:
    """Judges word things differently; citing the same evidence identifies the issue."""
    left = Finding(
        id="E-1",
        title="Headline latency reduction is contradicted by the results file",
        severity="critical",
        category="evidence",
        description="The paper reports 41ms; the benchmark records 48.3ms.",
        location="paper/main.md#results",
        evidence=["paper/main.md Results table: FastRoute 41ms", "results/benchmark.json: 48.3"],
        source="evidence",
    )
    right = Finding(
        id="A-1",
        title="Headline number disagrees with the supplied benchmark",
        severity="major",
        category="integrity",
        description="The p99 figure in the paper is not the figure in the artifact.",
        location="paper/main.md#results",
        evidence=["paper: 41ms", "results/benchmark.json: 48.3ms"],
        source="adversarial",
    )
    consolidated = consolidate([left, right])
    assert len(consolidated) == 1
    assert consolidated[0].finding.severity == "critical"
    assert set(consolidated[0].reported_by) == {"evidence", "adversarial"}


def test_unrelated_findings_at_one_location_are_not_merged() -> None:
    """Sharing a location is not enough; the evidence must actually overlap."""
    left = Finding(
        id="E-1",
        title="Reported number contradicts the benchmark file",
        severity="critical",
        category="evidence",
        description="The p99 figure disagrees with the artifact.",
        location="paper/main.md#results",
        evidence=["results/benchmark.json records 48.3ms"],
        source="evidence",
    )
    right = Finding(
        id="S-1",
        title="Table caption omits measurement units",
        severity="minor",
        category="clarity",
        description="The results table does not say which unit the column uses.",
        location="paper/main.md#results",
        evidence=["Table header reads only 'latency'"],
        source="structure",
    )
    assert len(consolidate([left, right])) == 2
