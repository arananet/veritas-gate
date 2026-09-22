"""Rich terminal output. The CLI experience is a first-class deliverable."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from veritas.models.evaluation import EvaluationResult, GateResult
from veritas.models.finding import Finding, severity_rank

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
}


class ConsoleReporter:
    """Streams progress while the run executes, then prints the summary."""

    def __init__(self, console: Console | None = None, *, quiet: bool = False) -> None:
        self.console = console or Console()
        self.quiet = quiet
        self._phase: str | None = None

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
            self._phase = phase
            heading = {
                "check": "Running deterministic checks...",
                "judge": "Running judges...",
                "claims": "Building claim graph...",
                "meta": "Meta review...",
            }.get(phase, phase)
            self.console.print(heading)
        if status == "start":
            return
        mark, style = STATUS_MARK.get(status, ("  ", ""))
        self.console.print(f"  [{style}]{mark}[/{style}]{name}")

    def summary(self, result: EvaluationResult) -> None:
        if self.quiet:
            self.console.print(result.gate.status)
            return
        self.console.print()
        self._judge_errors(result)
        self._claims(result)
        self._findings_table(result.gate)
        self._blocking(result)
        self._gate(result.gate)

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

    def _blocking(self, result: EvaluationResult) -> None:
        blocking = set(result.gate.blocking_findings)
        findings = [item for item in result.meta_review.findings if item.id in blocking]
        if not findings:
            return
        self.console.print("Blocking findings:")
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
        table.add_column("Severity")
        table.add_column("Title")
        table.add_column("Location", style="dim")
        for finding in findings:
            table.add_row(
                finding.id,
                Text(finding.severity.upper(), style=SEVERITY_STYLE[finding.severity]),
                finding.title,
                finding.location or "-",
            )
        self.console.print(table)

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
