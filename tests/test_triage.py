"""Triage: who acts on a finding, and what a declared limitation weighs."""

from __future__ import annotations

from pathlib import Path

from veritas.models.finding import Finding
from veritas.triage import declared, normalise, settle


def make(**overrides) -> Finding:
    base = {
        "id": "X-1",
        "title": "Topology confound",
        "severity": "major",
        "category": "methodology",
        "description": "Protocol effects cannot be separated from topology.",
        "evidence": ["Threats: 'protocol causality is not isolated'"],
    }
    return Finding(**{**base, **overrides})


def test_a_declared_limitation_with_quoted_evidence_is_capped_at_minor() -> None:
    """An honest paper was held at REVISE by limitations it had already declared."""
    capped = declared(make(disposition="declared"))
    assert capped.severity == "minor"
    assert capped.original_severity == "major"
    assert capped.disposition == "declared"


def test_declared_without_evidence_is_not_honoured() -> None:
    """Otherwise a judge could launder any defect by calling it declared."""
    result = declared(make(disposition="declared", evidence=[]))
    assert result.severity == "major"
    assert result.disposition == "artifact"


def test_a_critical_finding_is_never_capped() -> None:
    """Acknowledging a fabricated result does not make it acceptable."""
    result = declared(make(disposition="declared", severity="critical"))
    assert result.severity == "critical"


def test_an_undeclared_finding_is_untouched() -> None:
    finding = make()
    assert declared(finding) == finding


def test_unsupplied_paths_and_failed_judges_are_configuration() -> None:
    unsupplied = settle(
        make(category="check/reference-integrity/unsupplied", disposition="artifact")
    )
    failed = settle(make(category="judge-error", disposition="declared"))
    assert unsupplied.disposition == "configuration"
    assert failed.disposition == "configuration"


def test_an_unknown_disposition_falls_back_to_artifact() -> None:
    assert normalise("whatever") == "artifact"
    assert normalise(None) == "artifact"
    assert normalise("Decision") == "decision"


def test_findings_recorded_before_triage_still_load() -> None:
    legacy = Finding.model_validate(
        {"id": "A", "title": "t", "severity": "minor", "category": "c", "description": "d"}
    )
    assert legacy.disposition == "artifact"


# ------------------------------------------- next steps


def test_next_steps_group_by_who_acts_and_name_commands() -> None:
    from veritas.reports.next_steps import MAX_LINES, next_steps

    findings = [
        make(id="J", category="judge-error", disposition="configuration"),
        make(id="U", category="check/references/unsupplied", disposition="configuration"),
        make(id="A", title="Stale suite count", disposition="artifact"),
        make(id="D", title="No DOI", disposition="decision"),
        make(id="L", severity="minor", disposition="declared"),
    ]
    steps = next_steps(findings, "REVISE", can_repair=True)
    text = "\n".join(s.text for s in steps)
    commands = [s.command for s in steps if s.command]

    assert "did not complete" in text
    assert "artifact.paths" in text
    assert "blocking issue(s) are fixable" in text
    assert "No DOI" in text
    assert "declared limitation" in text
    assert "veritas evaluate ." in commands
    assert "veritas loop . --workspace worktree" in commands
    assert sum(1 + bool(s.command) for s in steps) <= MAX_LINES


def test_a_passing_gate_says_so_in_one_line() -> None:
    from veritas.reports.next_steps import next_steps

    steps = next_steps([], "PASS", can_repair=True)
    assert len(steps) == 1 and "Nothing blocks" in steps[0].text


def test_no_repair_command_is_offered_without_a_repair_agent() -> None:
    from veritas.reports.next_steps import next_steps

    steps = next_steps([make(disposition="artifact")], "REVISE", can_repair=False)
    assert all(s.command is None for s in steps)


# ------------------------------------------- the thesis


THESIS = ["Reusing an identifier overwrites in shared mappings."]


def test_the_thesis_reaches_the_judge_as_claims_to_evaluate_not_facts() -> None:
    from veritas.judges.base import EvaluationContext
    from veritas.judges.llm import LLMJudge

    judge = LLMJudge(name="methodology", prompt="Evaluate.", provider=None)  # type: ignore[arg-type]
    with_thesis = judge.system_prompt(
        EvaluationContext(profile="p", profile_version="1", run_id="r", thesis=THESIS)
    )
    without = judge.system_prompt(EvaluationContext(profile="p", profile_version="1", run_id="r"))

    assert THESIS[0] in with_thesis
    assert "not established by being listed" in with_thesis
    assert "narrowest wording" in with_thesis
    assert "INTENDED CLAIMS" not in without


def test_the_repair_prompt_says_preserve_and_bound_never_delete() -> None:
    from veritas.config import RepairAgentConfig
    from veritas.models.repair import RepairPlan
    from veritas.repair.cli_agent import GenericCLIRepairAgent
    from veritas.repair.permissions import RepairPermissions

    plan = RepairPlan(evaluation_run_id="r", iteration=1, actions=[])
    agent = GenericCLIRepairAgent(RepairAgentConfig(), RepairPermissions(), thesis=THESIS)
    prompt = agent.render_prompt(plan, Path("plan.json"), Path("result.json"))
    assert "Claims to preserve" in prompt
    assert THESIS[0] in prompt
    assert "do not delete the claim" in prompt

    bare = GenericCLIRepairAgent(RepairAgentConfig(), RepairPermissions())
    assert "Claims to preserve" not in bare.render_prompt(plan, Path("p"), Path("r"))


def test_accepted_findings_ask_nothing_more() -> None:
    """Next steps once nagged about exclusions the operator had already accepted."""
    from veritas.reports.next_steps import next_steps

    findings = [
        make(id="U", category="check/references/unsupplied", disposition="configuration"),
        make(id="A", disposition="artifact"),
    ]
    steps = next_steps(findings, "REVISE", can_repair=True, accepted={"U"}, blocking={"A"})
    text = " ".join(s.text for s in steps)
    assert "artifact.paths" not in text
    assert "1 blocking issue(s)" in text


def test_blocking_follows_the_gate_not_a_severity_guess() -> None:
    """It once reported four blocking issues beside a gate that listed one."""
    from veritas.reports.next_steps import next_steps

    findings = [make(id=f"A{i}", disposition="artifact") for i in range(4)]
    steps = next_steps(
        findings, "REVISE", can_repair=True, accepted={"A1", "A2", "A3"}, blocking={"A0"}
    )
    assert "1 blocking issue(s)" in " ".join(s.text for s in steps)


def test_missing_archival_files_point_to_scaffold() -> None:
    from veritas.reports.next_steps import next_steps

    steps = next_steps(
        [make(id="F", category="check/archival-files", severity="minor")],
        "REVISE",
        can_repair=False,
    )
    assert any(s.command == "veritas scaffold ." for s in steps)
