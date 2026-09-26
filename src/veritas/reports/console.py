"""Rich terminal output. The CLI experience is a first-class deliverable."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from veritas.models.evaluation import EvaluationResult, GateResult
from veritas.models.finding import Finding, issue_key, severity_rank
from veritas.reports.next_steps import next_steps
from veritas.reports.spinner import WorkSpinner
from veritas.usage import UsageSummary

SEVERITY_STYLE = {
    "critical": "bold red",
    "major": "red",
    "minor": "yellow",
    "info": "cyan",
}

GATE_STYLE = {
    "PASS": "bold green",
    "PASS_WITH_WARNINGS": "bold yellow",
    "REVISE": "bold yellow",
    "FAIL": "bold red",
}

STATUS_MARK = {
    "start": ("  ", "dim"),
    "ok": ("✓ ", "green"),
    "warn": ("! ", "yellow"),
    "fail": ("✗ ", "red"),
    # A verdict and a breakdown must never look alike.
    "error": ("⚠ ", "bold red"),
}


class ConsoleReporter:
    """Streams progress while the run executes, then prints the summary."""

    def __init__(self, console: Console | None = None, *, quiet: bool = False) -> None:
        self.console = console or Console()
        self.quiet = quiet
        self._phase: str | None = None
        self._spinner = WorkSpinner(self.console)

    def header(self, profile: str, artifact: str) -> None:
        if self.quiet:
            return
        self.console.print()
        self.console.print(Text("Veritas Gate", style="bold white"))
        self.console.print(Text("Trust, but verify.", style="dim"))
        self.console.print()
        self.console.print(f"Profile:  [bold]{profile}[/bold]")
        self.console.print(f"Artifact: [bold]{artifact}[/bold]")
        self.console.print()

    def external_paths_warning(self, paths: list[str], mode: str) -> None:
        """Say when configured paths reach outside the artifact directory.

        They are evaluated fine, but a repair workspace that copies only the
        artifact directory will not contain them, so the loop would repair an
        incomplete tree.
        """
        if self.quiet or not paths:
            return
        self.console.print(
            f"[yellow]note:[/yellow] {len(paths)} configured path(s) resolve outside "
            "the artifact directory:"
        )
        for path in paths:
            self.console.print(f"  [yellow]·[/yellow] {path}")
        if mode in ("copy", "snapshot"):
            self.console.print(
                f"[yellow]workspace.mode is '{mode}', which copies only the artifact "
                "directory, so `veritas loop` would not see these. Use 'worktree', or "
                "run from the directory that contains them.[/yellow]"
            )
        self.console.print()

    def truncation_warning(self, files: list[str], limit: int) -> None:
        """Say when the judges were shown only part of a file.

        A truncated manuscript means the judges reviewed a paper whose ending
        they never read; that must never be a silent condition.
        """
        if self.quiet or not files:
            return
        self.console.print(
            f"[yellow]warning:[/yellow] {len(files)} file(s) exceed "
            f"{limit:,} characters and reach the judges truncated:"
        )
        for path in files:
            self.console.print(f"  [yellow]![/yellow] {path}")
        self.console.print(
            "[dim]Raise artifact.max_file_chars, or split the file, "
            "so nothing is judged unread.[/dim]"
        )
        self.console.print()

    def progress(self, phase: str, name: str, status: str) -> None:
        if self.quiet:
            return
        if phase == "gate":
            # The gate gets its own panel in summary(); no progress line for it.
            return
        if phase != self._phase:
            self._spinner.stop()
            self._phase = phase
            heading = {
                "check": "Running deterministic checks...",
                "judge": "Running judges...",
                "claims": "Building claim graph...",
                "meta": "Meta review...",
            }.get(phase, phase)
            self.console.print(heading)
            self._spinner.start()
        if status == "start":
            self._spinner.mark_running(name)
            return
        mark, style = STATUS_MARK.get(status, ("  ", ""))
        self._spinner.mark_done(name, f"  [{style}]{mark}[/{style}]{name}")

    def summary(self, result: EvaluationResult, *, can_repair: bool = False) -> None:
        self._spinner.stop()
        if self.quiet:
            self.console.print(result.gate.status)
            return
        self.console.print()
        self._judge_errors(result)
        self._claims(result)
        self._accepted_risks(result.gate)
        self._findings_table(result.gate)
        self._usage(result)
        self._blocking(result)
        self._gate(result.gate)
        print_next_steps(self.console, result, can_repair=can_repair)

    def _judge_errors(self, result: EvaluationResult) -> None:
        """Say why a judge failed. A silent ✗ hid broken credentials as a pass."""
        failed = [item for item in result.judge_results if item.status == "error"]
        if not failed:
            return
        self.console.print(f"[bold red]{len(failed)} judge(s) could not complete:[/bold red]")
        for item in failed:
            self.console.print(f"  [red]✗[/red] {item.judge}")
            if item.summary:
                self.console.print(f"    [dim]{item.summary}[/dim]")
        self.console.print()
        self.console.print(
            "[yellow]The artifact was not fully evaluated, so the gate cannot pass.[/yellow]"
        )
        self.console.print()

    def _claims(self, result: EvaluationResult) -> None:
        coverage = result.coverage
        if not coverage.total:
            return
        self.console.print(f"{coverage.total} claims detected")
        self.console.print(f"  [green]✓ {coverage.verified} verified[/green]")
        self.console.print(
            f"  [yellow]! {coverage.partially_supported} partially supported[/yellow]"
        )
        self.console.print(f"  [red]✗ {coverage.unsupported} unsupported[/red]")
        if coverage.unverified:
            self.console.print(f"  [dim]? {coverage.unverified} unverified[/dim]")
        self.console.print(f"  evidence coverage: {coverage.coverage}%")
        self.console.print()

    def _accepted_risks(self, gate: GateResult) -> None:
        """Name what was carried deliberately, and what has gone stale."""
        if gate.accepted_risks:
            self.console.print(
                f"[cyan]{len(gate.accepted_risks)} rule(s) accepted "
                f"{len(gate.accepted_findings)} finding(s) as known risk:[/cyan]"
            )
            for rule in gate.accepted_risks:
                self.console.print(f"  [cyan]·[/cyan] {rule}")
            if len(gate.accepted_findings) > len(gate.accepted_risks):
                # A rule written by category can cover far more than intended.
                self.console.print(
                    "  [dim]covering: " + ", ".join(gate.accepted_findings) + "[/dim]"
                )
        if gate.stale_accepted_risks:
            self.console.print(
                f"[yellow]{len(gate.stale_accepted_risks)} accepted risk(s) match nothing "
                "or have expired:[/yellow] " + ", ".join(gate.stale_accepted_risks)
            )
            self.console.print(
                "[dim]Remove them from gate.accepted_risks, or they will outlive "
                "the problem they excused.[/dim]"
            )
        if gate.accepted_risks or gate.stale_accepted_risks:
            self.console.print()

    def _findings_table(self, gate: GateResult) -> None:
        table = Table(show_header=False, box=None, pad_edge=False)
        table.add_column("severity", style="bold", width=10)
        table.add_column("count", justify="right")
        table.add_row(Text("CRITICAL", style=SEVERITY_STYLE["critical"]), str(gate.critical))
        table.add_row(Text("MAJOR", style=SEVERITY_STYLE["major"]), str(gate.major))
        table.add_row(Text("MINOR", style=SEVERITY_STYLE["minor"]), str(gate.minor))
        table.add_row(Text("INFO", style=SEVERITY_STYLE["info"]), str(gate.info))
        self.console.print("Findings:")
        self.console.print(table)
        self.console.print()

    def _usage(self, result: EvaluationResult) -> None:
        """What the run spent: which models ran, their tokens, and cost if priced."""
        summary = UsageSummary.model_validate(result.manifest.usage or {})
        if not summary.by_model and not summary.calls:
            return

        table = Table(show_header=True, box=None, pad_edge=False, header_style="dim")
        table.add_column("model")
        table.add_column("calls", justify="right")
        table.add_column("in", justify="right")
        table.add_column("out", justify="right")
        if summary.has_cost:
            table.add_column("cost", justify="right")

        for entry in summary.by_model:
            row = [
                entry.model,
                str(entry.calls),
                f"{entry.input_tokens:,}",
                f"{entry.output_tokens:,}",
            ]
            if summary.has_cost:
                row.append(_money(entry.cost, summary.currency))
            table.add_row(*row)

        total = [
            "[bold]total[/bold]",
            str(summary.calls),
            f"{summary.input_tokens:,}",
            f"{summary.output_tokens:,}",
        ]
        if summary.has_cost:
            total.append(f"[bold]{_money(summary.cost, summary.currency)}[/bold]")
        table.add_row(*total)

        self.console.print("Usage:")
        self.console.print(table)
        if summary.unpriced_models:
            names = ", ".join(summary.unpriced_models)
            self.console.print(
                f"  [yellow]no price configured for {names}; the total is incomplete[/yellow]"
            )
        if summary.calls_without_usage:
            self.console.print(
                f"  [yellow]{summary.calls_without_usage} call(s) reported no usage metadata; "
                "token counts are a floor[/yellow]"
            )
        if summary.has_cost:
            self.console.print(
                "  [dim]estimated from your configured rates, not a billing record[/dim]"
            )
        self.console.print()

    def _blocking(self, result: EvaluationResult) -> None:
        blocking = set(result.gate.blocking_findings)
        findings = [item for item in result.meta_review.findings if item.id in blocking]
        if not findings:
            return
        passed = result.gate.status == "PASS_WITH_WARNINGS"
        self.console.print("Warnings (not blocking):" if passed else "Blocking findings:")
        self.console.print()
        for finding in sorted(findings, key=lambda item: -severity_rank(item.severity)):
            style = SEVERITY_STYLE[finding.severity]
            self.console.print(f"  [{style}][{finding.id}][/{style}] {finding.title}")
            if finding.location:
                self.console.print(f"    [dim]{finding.location}[/dim]")
        self.console.print()

    def _gate(self, gate: GateResult) -> None:
        style = GATE_STYLE[gate.status]
        self.console.print(
            Panel(
                Text(gate.status, style=style),
                title="Gate",
                border_style=style,
                expand=False,
            )
        )

    def findings_list(self, findings: list[Finding]) -> None:
        if not findings:
            self.console.print("No findings.")
            return
        table = Table(box=None, pad_edge=False)
        table.add_column("ID", style="bold")
        table.add_column("Issue", style="dim")
        table.add_column("Severity")
        table.add_column("Title")
        table.add_column("Location", style="dim")
        for finding in findings:
            table.add_row(
                finding.id,
                issue_key(finding),
                Text(finding.severity.upper(), style=SEVERITY_STYLE[finding.severity]),
                finding.title,
                finding.location or "-",
            )
        self.console.print(table)
        self.console.print()
        self.console.print(
            "[dim]ID changes between runs; Issue is stable — it is what you name in "
            "gate.accepted_risks.[/dim]"
        )

    def claims_list(self, result: EvaluationResult) -> None:
        coverage = result.coverage
        self.console.print(f"Claims: {coverage.total}")
        self.console.print()
        self.console.print(f"Verified:             {coverage.verified}")
        self.console.print(f"Partially supported:  {coverage.partially_supported}")
        self.console.print(f"Unsupported:          {coverage.unsupported}")
        self.console.print(f"Unverified:           {coverage.unverified}")
        self.console.print()
        self.console.print(f"Evidence coverage:    {coverage.coverage}%")
        if not result.claims:
            return
        self.console.print()
        table = Table(box=None, pad_edge=False)
        table.add_column("ID", style="bold")
        table.add_column("Status")
        table.add_column("Claim")
        table.add_column("Location", style="dim")
        status_style = {
            "verified": "green",
            "partially-supported": "yellow",
            "unsupported": "red",
            "unverified": "dim",
        }
        for claim in result.claims:
            table.add_row(
                claim.id,
                Text(claim.status, style=status_style[claim.status]),
                claim.text[:80],
                claim.source_location or "-",
            )
        self.console.print(table)


def _money(value: float | None, currency: str | None) -> str:
    if value is None:
        return "—"
    unit = f" {currency}" if currency and currency != "USD" else ""
    prefix = "$" if not unit else ""
    return f"{prefix}{value:,.4f}{unit}"


def print_next_steps(console: Console, result: EvaluationResult, *, can_repair: bool) -> None:
    """Say what to do next, grouped by who acts, in a handful of lines."""
    steps = next_steps(
        result.meta_review.findings,
        result.gate.status,
        can_repair=can_repair,
        accepted=set(result.gate.accepted_findings),
        blocking=set(result.gate.blocking_findings),
    )
    if not steps:
        return
    console.print()
    console.print("[bold]Next steps[/bold]")
    for index, step in enumerate(steps, start=1):
        console.print(f"  {index}. {step.text}")
        if step.command:
            console.print(f"     [cyan]{step.command}[/cyan]", soft_wrap=True)
