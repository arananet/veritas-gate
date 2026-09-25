"""Loop reporting: the autopilot console experience and the final report."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from veritas.models.evaluation import EvaluationResult, GateResult
from veritas.models.loop import STOP_REASON_TEXT, EvaluationDelta, LoopResult, StopReason
from veritas.models.repair import RepairPlan, RepairResult
from veritas.reports.console import GATE_STYLE, SEVERITY_STYLE, STATUS_MARK, print_next_steps
from veritas.reports.spinner import WorkSpinner

STOP_STYLE: dict[StopReason, str] = {
    StopReason.QUALITY_GATE_REACHED: "bold green",
    StopReason.HUMAN_DECISION_REQUIRED: "bold yellow",
    StopReason.DRY_RUN: "bold cyan",
}


class LoopReporter:
    """Streams the autopilot run as it happens."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        quiet: bool = False,
        verbose: bool = False,
    ) -> None:
        self.console = console or Console()
        self.quiet = quiet
        self.verbose = verbose
        self._engine_phase: str | None = None
        self._spinner = WorkSpinner(self.console)
        self._last_evaluation: Any = None

    def header(self, profile: str, artifact: str, mode: str, max_iterations: int) -> None:
        if self.quiet:
            return
        title = "Veritas Autopilot" if mode == "autopilot" else "Veritas Autopilot (dry run)"
        self.console.print()
        self.console.print(Text(title, style="bold white"))
        self.console.print()
        self.console.print(f"Profile:  [bold]{profile}[/bold]")
        self.console.print(f"Artifact: [bold]{artifact}[/bold]")
        self.console.print(f"Budget:   up to {max_iterations} iteration(s)")
        self.console.print()

    def left_out_warning(self, paths: list[str], limit: int = 12) -> None:
        """Name the files a worktree cannot see before evaluating without them.

        A worktree holds only tracked files. Untracked or ignored evidence
        exists on disk and is absent from what the loop evaluates, so an
        evaluation and a loop over the same repository disagreed, and neither
        said why. The omission is often the real finding: a reviewer who
        clones the repository gets the worktree, not the operator's disk.
        """
        if self.quiet or not paths:
            return
        self.console.print(
            f"[yellow]{len(paths)} file(s) in the artifact differ from the last commit "
            "(untracked, ignored or uncommitted), and this worktree is built from "
            "the commit:[/yellow]"
        )
        for path in paths[:limit]:
            self.console.print(f"  [dim]{_escape(path)}[/dim]")
        if len(paths) > limit:
            self.console.print(f"  [dim]... and {len(paths) - limit} more[/dim]")
        self.console.print(
            "  [dim]Judges will evaluate the committed version, or nothing. Commit "
            "before running the loop, or use --workspace copy.[/dim]"
        )
        self.console.print()

    def engine_progress(self, phase: str, name: str, status: str) -> None:
        """Relay the evaluation engine's own per-judge/check progress.

        Without this, an iteration goes silent for however long the judges
        take -- a minute or more against a real paper -- which is
        indistinguishable from a hang.
        """
        if self.quiet:
            return
        if phase != self._engine_phase:
            self._spinner.stop()
            self._engine_phase = phase
            heading = {
                "check": "  Running deterministic checks...",
                "judge": "  Running judges...",
                "claims": "  Building claim graph...",
                "meta": "  Meta review...",
            }.get(phase, f"  {phase}...")
            self.console.print(heading)
            self._spinner.start()
        if status == "start":
            self._spinner.mark_running(name)
            return
        mark, style = STATUS_MARK.get(status, ("  ", ""))
        self._spinner.mark_done(name, f"    [{style}]{mark}[/{style}]{name}")

    def progress(self, event: str, payload: dict[str, Any]) -> None:
        if self.quiet:
            return
        handler = {
            "iteration": self._iteration,
            "evaluated": self._evaluated,
            "planned": self._planned,
            "repairing": self._repairing,
            "repaired": self._repaired,
            "delta": self._delta,
        }.get(event)
        if handler is not None:
            handler(payload)

    # ------------------------------------------------------------- events

    def _iteration(self, payload: dict[str, Any]) -> None:
        self._spinner.stop()
        self._engine_phase = None
        self.console.rule(
            f"[bold]Iteration {payload['iteration']} / {payload['total']}[/bold]",
            style="dim",
        )

    def _evaluated(self, payload: dict[str, Any]) -> None:
        self._last_evaluation = payload.get("result")
        gate: GateResult = payload["gate"]
        self.console.print("Evaluating...")
        self._judge_errors(payload.get("result"))
        self.console.print(
            f"  [{SEVERITY_STYLE['critical']}]Critical: {gate.critical}[/]"
            f"  [{SEVERITY_STYLE['major']}]Major: {gate.major}[/]"
            f"  [{SEVERITY_STYLE['minor']}]Minor: {gate.minor}[/]"
        )
        style = GATE_STYLE[gate.status]
        self.console.print(f"  Gate: [{style}]{gate.status}[/{style}]")
        self.console.print()

    def _judge_errors(self, evaluation: Any) -> None:
        """Say which judges could not complete, and why.

        A judge that broke and a judge that found blocking problems both
        streamed as a red mark, so a degraded panel read as a thorough one.
        """
        results = getattr(evaluation, "judge_results", None) or []
        failed = [item for item in results if item.status == "error"]
        if not failed:
            return
        self.console.print(
            f"  [bold red]{len(failed)} judge(s) could not complete "
            "-- this evaluation is incomplete:[/bold red]"
        )
        for item in failed:
            self.console.print(f"    [bold red]⚠[/bold red] {item.judge}")
            if item.summary:
                self.console.print(f"      [dim]{item.summary}[/dim]")

    def _planned(self, payload: dict[str, Any]) -> None:
        plan: RepairPlan = payload["plan"]
        autonomous = len(plan.autonomous_actions)
        human = len(plan.human_actions)
        self.console.print("Repair plan:")
        self.console.print(f"  {autonomous} autonomous action(s)")
        if human:
            self.console.print(f"  [yellow]{human} human-only action(s)[/yellow]")
        self.console.print()

    def _repairing(self, payload: dict[str, Any]) -> None:
        self.console.print("Applying autonomous repairs...")
        self._spinner.start()

    def agent_output(self, line: str) -> None:
        """Relay one line of the configured repair agent's own output.

        Veritas neither parses nor interprets it: this is whatever Codex,
        Claude Code, a Copilot CLI or any other configured tool chose to print.
        """
        if self.quiet:
            return
        if self.verbose:
            self._spinner.mark_done(line, f"    [dim]{_escape(line)}[/dim]")
            return
        self._spinner.note(line)

    def _repaired(self, payload: dict[str, Any]) -> None:
        self._spinner.clear_note()
        repair: RepairResult = payload["repair"]
        for action_id in repair.action_ids:
            mark = "✓" if repair.status == "completed" else "!"
            style = "green" if repair.status == "completed" else "yellow"
            self.console.print(f"  [{style}]{mark}[/{style}] {action_id}")
        changed = payload.get("changed") or []
        if changed:
            self.console.print(f"  [dim]{len(changed)} file(s) changed[/dim]")
        # An agent that declined actions usually says why. Those notes were
        # written to disk and never shown, leaving a column of bare "!" marks
        # as the only account of what happened.
        if repair.status != "completed" and repair.notes:
            self.console.print()
            self.console.print(f"  [yellow]The agent reported status '{repair.status}':[/yellow]")
            for note in repair.notes:
                self.console.print(f"    [dim]· {_escape(note)}[/dim]")
        self.console.print()

    def _delta(self, payload: dict[str, Any]) -> None:
        delta: EvaluationDelta = payload["delta"]
        self.console.print("Re-evaluating...")
        self.console.print(f"  [green]Resolved:   {len(delta.resolved)}[/green]")
        self.console.print(f"  [cyan]Improved:   {len(delta.improved)}[/cyan]")
        self.console.print(f"  Unchanged:  {len(delta.unchanged)}")
        self.console.print(f"  [red]Regressed:  {len(delta.regressed)}[/red]")
        self.console.print(f"  [yellow]New:        {len(delta.new_findings)}[/yellow]")
        self.console.print()

    # ------------------------------------------------------------ summary

    def summary(self, result: LoopResult) -> None:
        self._spinner.stop()
        if self.quiet:
            self.console.print(result.stop_reason.value)
            return
        style = STOP_STYLE.get(result.stop_reason, "bold red")
        self.console.print()
        self.console.print(
            Panel(
                Text(result.stop_reason.value.upper(), style=style),
                title="STOP",
                border_style=style,
                expand=False,
            )
        )
        self.console.print()
        self.console.print(result.stop_detail or STOP_REASON_TEXT.get(result.stop_reason, ""))
        self.console.print()

        if result.final_gate is not None:
            gate_style = GATE_STYLE[result.final_gate.status]
            self.console.print(
                f"Final gate: [{gate_style}]{result.final_gate.status}[/{gate_style}]"
            )
        self.console.print(f"Iterations: {len(result.iterations)}")
        if result.files_changed:
            self.console.print(f"Files changed: {len(result.files_changed)}")

        blockers = [
            entry
            for entry in result.ledger
            if entry.status in ("OPEN", "UNCHANGED", "REGRESSED", "HUMAN_REVIEW") and entry.blocking
        ]
        if blockers:
            self.console.print()
            self.console.print("Remaining blockers:")
            for entry in blockers:
                self.console.print(
                    f"  [{SEVERITY_STYLE[entry.severity]}]{entry.id}[/] {entry.title}"
                )
                if entry.notes:
                    self.console.print(f"    [dim]{entry.notes[-1]}[/dim]")

        if result.stop_reason is StopReason.HUMAN_DECISION_REQUIRED:
            self.console.print()
            self.console.print(
                "[yellow]Autonomous generation of experimental evidence is prohibited.[/yellow]"
            )
            self.console.print(
                "Do the work by hand, then resume with: [bold]veritas loop . --resume[/bold]"
            )

        if result.workspace and result.mode == "autopilot":
            self.console.print()
            if result.files_changed:
                self._how_to_apply(result)
            else:
                self.console.print(f"Workspace:\n  {result.workspace}")

        # The loop already repaired what it could; what remains is configuration
        # or a decision, so the next step is never "run the loop again" here.
        if self._last_evaluation is not None:
            print_next_steps(self.console, self._last_evaluation, can_repair=False)

    def _how_to_apply(self, result: LoopResult) -> None:
        """Say how to read the repair and how to take it, in full or in part.

        Veritas never writes to your tree: the repairer proposes, you accept.
        Naming the workspace is not enough -- the commands are what an operator
        actually needs, and accepting only some of a repair is the common case,
        not the exception.
        """
        workspace = result.workspace
        files = result.files_changed
        self.console.print(
            f"[bold]{len(files)} file(s) changed, in a workspace, not in your tree:[/bold]"
        )
        for path in files:
            self.console.print(f"  [dim]{path}[/dim]")
        self.console.print()
        self.console.print("  Review the whole repair:")
        self._command(f"git -C {workspace} diff")
        self.console.print()
        self.console.print("  Review one file:")
        self._command(f"git -C {workspace} diff -- {files[0]}")
        self.console.print()
        self.console.print("  Take all of it (run from your repository root):")
        self._command(f"git -C {workspace} diff | git apply --3way")
        if len(files) > 1:
            self.console.print()
            self.console.print("  Take only the files you accept:")
            kept = " ".join(files[:2])
            self._command(f"git -C {workspace} diff -- {kept} | git apply --3way")
        self.console.print()
        self.console.print(
            "  [dim]Nothing is applied until you run one of these. Commit your own "
            "changes first:[/dim]"
        )
        self.console.print("  [dim]a three-way apply refuses files with uncommitted edits.[/dim]")

    def _command(self, text: str) -> None:
        """Print a command unwrapped: a path broken across lines cannot be pasted."""
        self.console.print(f"    [cyan]{text}[/cyan]", soft_wrap=True)

    def plan_preview(self, plan: RepairPlan) -> None:
        """Print a plan without applying it (assist mode and dry runs)."""
        if not plan.actions:
            self.console.print("No repair actions were derived from the findings.")
            return
        table = Table(box=None, pad_edge=False)
        table.add_column("ID", style="bold")
        table.add_column("Priority")
        table.add_column("Type")
        table.add_column("Findings", style="dim")
        table.add_column("Autonomous")
        for action in plan.actions:
            table.add_row(
                action.id,
                action.priority,
                action.action_type,
                ", ".join(action.finding_ids),
                Text("yes", style="green") if action.autonomous else Text("human", style="yellow"),
            )
        self.console.print(table)
        blocked = [action for action in plan.actions if not action.autonomous]
        if blocked:
            self.console.print()
            for action in blocked:
                self.console.print(f"[yellow]{action.id}[/yellow]: {action.blocked_reason}")

    def diff(self, delta: EvaluationDelta) -> None:
        for label, items, style in (
            ("Resolved", delta.resolved, "green"),
            ("Improved", delta.improved, "cyan"),
            ("Unchanged", delta.unchanged, ""),
            ("Regressed", delta.regressed, "red"),
            ("New", delta.new_findings, "yellow"),
        ):
            self.console.print(f"[{style}]{label}:[/{style}]" if style else f"{label}:")
            for item in items:
                self.console.print(f"  {item}")
            if not items:
                self.console.print("  none")
            self.console.print()


def render_loop_report(result: LoopResult, evaluation: EvaluationResult | None = None) -> str:
    """The final Markdown report for a loop."""
    lines: list[str] = [
        "# Veritas Gate — Loop Report",
        "",
        f"**Stop reason:** `{result.stop_reason.value}`",
        "",
        result.stop_detail or STOP_REASON_TEXT.get(result.stop_reason, ""),
        "",
        "## Summary",
        "",
        f"- Loop id: `{result.loop_id}`",
        f"- Profile: {result.profile}",
        f"- Artifact: {result.artifact_id}",
        f"- Mode: {result.mode}",
        f"- Iterations: {len(result.iterations)}",
        f"- Initial gate: {result.initial_gate.status if result.initial_gate else 'n/a'}",
        f"- Final gate: {result.final_gate.status if result.final_gate else 'n/a'}",
        f"- Workspace: {result.workspace or 'in place'}",
        f"- Original commit: {result.original_commit or 'not a git checkout'}",
        f"- Started: {result.started_at.isoformat()}",
        f"- Finished: {result.finished_at.isoformat() if result.finished_at else 'n/a'}",
        "",
        "## Iterations",
        "",
        "| # | Gate | Actions planned | Autonomous | Repair | Resolved | Regressed | New |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in result.iterations:
        plan = record.plan
        delta = record.delta
        lines.append(
            f"| {record.iteration} | {record.gate.status} "
            f"| {len(plan.actions) if plan else 0} "
            f"| {len(plan.autonomous_actions) if plan else 0} "
            f"| {record.repair.status if record.repair else '-'} "
            f"| {len(delta.resolved) if delta else '-'} "
            f"| {len(delta.regressed) if delta else '-'} "
            f"| {len(delta.new_findings) if delta else '-'} |"
        )
    lines.append("")

    lines.extend(["## Findings ledger", ""])
    if result.ledger:
        lines.extend(
            [
                "| Issue | Status | Severity | Title | Iterations |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for entry in result.ledger:
            iterations = ", ".join(str(item) for item in entry.seen_in_iterations)
            lines.append(
                f"| {entry.id} | {entry.status} | {entry.severity.upper()} "
                f"| {entry.title} | {iterations} |"
            )
    else:
        lines.append("No findings were recorded.")
    lines.append("")

    resolved = [entry for entry in result.ledger if entry.status == "RESOLVED"]
    remaining = [
        entry for entry in result.ledger if entry.status not in ("RESOLVED", "ACCEPTED_RISK")
    ]
    lines.extend(
        [
            "## Outcome",
            "",
            f"- Resolved: {len(resolved)}",
            f"- Remaining: {len(remaining)}",
            f"- Files changed: {len(result.files_changed)}",
            "",
        ]
    )
    if result.files_changed:
        lines.extend([f"  - `{path}`" for path in result.files_changed])
        lines.append("")

    lines.extend(["## Repair actions", ""])
    any_action = False
    for record in result.iterations:
        if record.plan is None:
            continue
        any_action = True
        lines.append(f"### Iteration {record.iteration}")
        lines.append("")
        for action in record.plan.actions:
            status = "autonomous" if action.autonomous else "human decision required"
            lines.append(
                f"- **{action.id}** ({action.priority}, {action.action_type}, {status}) — "
                f"findings {', '.join(action.finding_ids)}"
            )
            if action.blocked_reason:
                lines.append(f"  - {action.blocked_reason}")
        lines.append("")
    if not any_action:
        lines.extend(["No repair actions were planned.", ""])

    if result.human_decisions:
        lines.extend(
            [
                "## Human decisions required",
                "",
                *[f"- {item}" for item in result.human_decisions],
                "",
                "Veritas may improve how existing evidence is represented, implemented,",
                "documented or validated. It will not fabricate missing evidence to satisfy",
                "its own evaluator.",
                "",
            ]
        )

    lines.extend(["## Usage", "", f"- {result.usage}", ""])
    return "\n".join(lines).rstrip() + "\n"


def _escape(text: str) -> str:
    """Markup in an agent's output is text, not formatting."""
    return text.replace("[", "\\[")
