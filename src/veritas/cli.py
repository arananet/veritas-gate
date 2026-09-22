"""The ``veritas`` command line.

Exit codes are meaningful so CI can gate on them:
``0`` pass, ``1`` pass with warnings, ``2`` revise, ``3`` fail, ``4`` usage or
configuration error.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from veritas import __version__
from veritas.artifacts.base import Artifact
from veritas.config import CONFIG_FILENAME, ConfigError, find_config, load_config
from veritas.engine import Engine, EngineOptions
from veritas.loop import LoopOptions, LoopOrchestrator, LoopStore, compare_evaluations
from veritas.loop.storage import latest_loop_dir
from veritas.models.evaluation import EvaluationResult
from veritas.models.finding import severity_rank
from veritas.profiles import Profile, available_profiles, load_profile, load_profile_dir
from veritas.providers.base import ProviderError
from veritas.repair import RepairPlanner, build_repair_agent, open_workspace
from veritas.reports import ConsoleReporter, LoopReporter, render_loop_report, render_markdown
from veritas.runs import latest_run_dir, load_run, unique_run_dir, write_run

USAGE_ERROR = 4

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Veritas Gate — independent evaluation and quality gates for artifacts.",
)
console = Console()
error_console = Console(stderr=True)


def _fail(message: str) -> typer.Exit:
    error_console.print(f"[bold red]error:[/bold red] {message}")
    return typer.Exit(USAGE_ERROR)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"veritas-gate {__version__}")
        raise typer.Exit(0)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Veritas Gate."""


def _resolve_profile(name: str, root: Path, extra: list[str], explicit: Path | None) -> Profile:
    if explicit is not None:
        return load_profile_dir(explicit)
    return load_profile(name, root, extra)


def _load_latest(root: Path) -> tuple[Path, EvaluationResult]:
    config_path = find_config(root)
    config = load_config(config_path, root=root if config_path is None else config_path.parent)
    run_dir = latest_run_dir(config.runs_dir())
    if run_dir is None:
        raise _fail(
            f"no evaluation runs found under {config.runs_dir()}. Run `veritas evaluate` first."
        )
    return run_dir, load_run(run_dir)


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
    profile: Annotated[str, typer.Option(help="Profile to configure.")] = "scientific-paper",
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config.")] = False,
) -> None:
    """Create ``veritas.yaml`` and the ``.veritas/`` run directory."""
    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / CONFIG_FILENAME
    if config_path.exists() and not force:
        raise _fail(f"{config_path} already exists. Pass --force to overwrite it.")

    catalog = available_profiles(root)
    if profile not in catalog:
        known = ", ".join(sorted(catalog)) or "(none found)"
        raise _fail(f"unknown profile '{profile}'. Available: {known}")

    config_path.write_text(_starter_config(profile), encoding="utf-8")
    runs = root / ".veritas" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    gitignore = root / ".veritas" / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("runs/\n", encoding="utf-8")

    console.print(f"[green]✓[/green] wrote {config_path}")
    console.print(f"[green]✓[/green] created {runs}")
    console.print()
    console.print("Next: export your provider API key (see .env.example), then run:")
    console.print("  [bold]veritas evaluate .[/bold]")


def _starter_config(profile: str) -> str:
    """The config `veritas init` writes.

    Every provider and model comes from the environment, so one `.env` drives
    every project. Nothing here is a secret: only variable names and policy.
    """
    return f"""\
version: 1

profile: {profile}

artifact:
  # What the judges read. Point this at the manuscript, the implementation and
  # the evidence behind your numbers: a claim whose supporting file is not
  # listed here cannot be verified, and will be reported as unverified.
  paths:
    - .

# Secrets never live here. Only environment variable names and model ids do.
# Copy .env.example from the Veritas repository and set the values there.
models:
  default:
    provider: ${{VERITAS_DEFAULT_PROVIDER:-anthropic}}
    model: ${{VERITAS_DEFAULT_MODEL}}
    tls_verify: ${{VERITAS_TLS_VERIFY:-true}}

  # A different vendor on purpose, so one model's blind spots do not decide the
  # outcome. Point it at the default provider if you only have one key.
  adversarial:
    provider: ${{VERITAS_ADVERSARIAL_PROVIDER:-openai}}
    model: ${{VERITAS_ADVERSARIAL_MODEL}}
    tls_verify: ${{VERITAS_TLS_VERIFY:-true}}

  # Consolidates the other judges' results. Optional: without it the meta
  # review still runs, deterministically, and only the narrative summary is lost.
  meta:
    provider: ${{VERITAS_META_PROVIDER:-google}}
    model: ${{VERITAS_META_MODEL}}
    tls_verify: ${{VERITAS_TLS_VERIFY:-true}}

gate:
  fail_on:
    - critical
  max_major: 0

# Nothing executes unless it is listed here.
execution:
  allow: []
"""


@app.command()
def profiles(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
) -> None:
    """List every profile visible from this project."""
    root = path.resolve()
    catalog = available_profiles(root)
    if not catalog:
        console.print("No profiles found.")
        raise typer.Exit(0)
    for name, directory in sorted(catalog.items()):
        try:
            loaded = load_profile_dir(directory)
        except ConfigError as exc:
            console.print(f"[red]{name}[/red] — invalid ({exc})")
            continue
        judges = ", ".join(spec.name for spec in loaded.definition.judges) or "(none)"
        console.print(f"[bold]{name}[/bold] v{loaded.version} — {loaded.definition.description}")
        console.print(f"  judges: {judges}")
        console.print(f"  path:   {directory}")


@app.command()
def files(
    path: Annotated[Path, typer.Argument(help="Artifact path.")] = Path("."),
    profile: Annotated[str | None, typer.Option(help="Profile name to use.")] = None,
    config: Annotated[Path | None, typer.Option(help="Path to veritas.yaml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show exactly what the judges would read, and roughly what it costs.

    Worth running before the first evaluation on a real project: every file
    listed here is sent to every judge, so this is the difference between
    evaluating your paper and evaluating your entire working directory.
    """
    target = path.resolve()
    if not target.exists():
        raise _fail(f"{target} does not exist")
    loaded_config, loaded_profile = _prepare(target, profile, config)
    artifact = _artifact_for(target, loaded_config, loaded_profile)
    segments = artifact.segments()

    judges = [spec.name for spec in loaded_profile.definition.judges if spec.enabled]
    total_chars = sum(len(segment.text) for segment in segments)
    # Deliberately rough, and rounded down in the wording: this is a sense of
    # scale before spending money, not a billing estimate.
    per_judge_tokens = total_chars // 4
    limit = 24_000

    if json_output:
        console.print_json(
            json.dumps(
                {
                    "artifact": str(target),
                    "profile": loaded_profile.name,
                    "paths": artifact.paths,
                    "files": [
                        {"path": segment.path, "characters": len(segment.text)}
                        for segment in segments
                    ],
                    "judges": judges,
                    "approximate_tokens_per_judge": per_judge_tokens,
                }
            )
        )
        return

    console.print(f"Artifact: [bold]{target}[/bold]")
    console.print(f"Profile:  [bold]{loaded_profile.name}[/bold]")
    console.print(f"Paths:    {', '.join(artifact.paths)}")
    console.print()

    if not segments:
        console.print("[yellow]No readable files matched.[/yellow]")
        console.print("Veritas reads text formats only. Check artifact.paths in your veritas.yaml.")
        raise typer.Exit(0)

    table = Table(box=None, pad_edge=False)
    table.add_column("File")
    table.add_column("Characters", justify="right")
    table.add_column("", style="yellow")
    for segment in sorted(segments, key=lambda item: -len(item.text)):
        size = len(segment.text)
        note = f"truncated at {limit:,}" if size > limit else ""
        table.add_row(segment.path, f"{size:,}", note)
    console.print(table)
    console.print()
    console.print(f"{len(segments)} file(s), {total_chars:,} characters")
    console.print(
        f"Roughly {per_judge_tokens:,} input tokens [bold]per judge[/bold], "
        f"and this profile runs {len(judges)} of them."
    )
    console.print("[dim]Skipped: binaries, PDFs and any format Veritas cannot read as text.[/dim]")


@app.command()
def evaluate(
    path: Annotated[Path, typer.Argument(help="Artifact path.")] = Path("."),
    profile: Annotated[str | None, typer.Option(help="Profile name to use.")] = None,
    profile_path: Annotated[
        Path | None, typer.Option(help="Load a profile from this directory.")
    ] = None,
    config: Annotated[Path | None, typer.Option(help="Path to veritas.yaml.")] = None,
    judge: Annotated[
        list[str] | None, typer.Option("--judge", help="Run only these judges (repeatable).")
    ] = None,
    runs: Annotated[int, typer.Option(help="Repeat each judge N times to measure stability.")] = 1,
    skip_checks: Annotated[
        bool, typer.Option("--skip-checks", help="Skip deterministic checks.")
    ] = False,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Print only the gate status.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print results.json to stdout.")
    ] = False,
) -> None:
    """Evaluate an artifact and write an immutable run."""
    target = path.resolve()
    if not target.exists():
        raise _fail(f"{target} does not exist")

    config_path = config.resolve() if config else find_config(target)
    try:
        loaded_config = load_config(
            config_path, root=target if config_path is None else config_path.parent
        )
    except ConfigError as exc:
        raise _fail(str(exc)) from exc

    profile_name = profile or loaded_config.profile
    try:
        loaded_profile = _resolve_profile(
            profile_name, loaded_config.root, loaded_config.profile_paths, profile_path
        )
    except ConfigError as exc:
        raise _fail(str(exc)) from exc

    paths = loaded_config.artifact.paths or loaded_profile.definition.default_paths
    if target.is_file():
        artifact_root = target.parent
        paths = [target.name]
    else:
        artifact_root = target

    artifact = Artifact(
        id=target.name or str(target),
        type=loaded_config.artifact.type or loaded_profile.definition.artifact_type,
        root=artifact_root,
        paths=paths,
    )

    reporter = ConsoleReporter(console, quiet=quiet or json_output)
    reporter.header(loaded_profile.name, str(target))
    options = EngineOptions(
        judge_filter=list(judge or []),
        runs=runs,
        skip_checks=skip_checks,
        progress=reporter.progress,
    )
    engine = Engine(loaded_config, loaded_profile, options)

    try:
        result = asyncio.run(engine.run(artifact))
    except (ConfigError, ProviderError) as exc:
        raise _fail(str(exc)) from exc

    markdown = render_markdown(result)
    run_dir = unique_run_dir(loaded_config.runs_dir(), result.manifest.run_id)
    write_run(run_dir, result, markdown)
    (loaded_config.root / ".veritas" / "report.md").write_text(markdown, encoding="utf-8")

    if json_output:
        console.print_json(json.dumps(result.model_dump(mode="json")))
    else:
        reporter.summary(result)
        if not quiet:
            console.print(f"[dim]run: {run_dir}[/dim]")

    raise typer.Exit(result.gate.exit_code)


@app.command()
def report(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
    raw: Annotated[bool, typer.Option("--raw", help="Print the raw Markdown.")] = False,
) -> None:
    """Show the report from the latest run."""
    run_dir, _ = _load_latest(path.resolve())
    markdown = (run_dir / "report.md").read_text(encoding="utf-8")
    if raw:
        typer.echo(markdown)
        return
    from rich.markdown import Markdown

    console.print(Markdown(markdown))


@app.command()
def findings(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
    severity: Annotated[
        str | None, typer.Option(help="Show findings at this severity or worse.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List the findings from the latest run."""
    _, result = _load_latest(path.resolve())
    items = result.meta_review.findings
    if severity:
        if severity not in {"info", "minor", "major", "critical"}:
            raise _fail(f"unknown severity '{severity}'")
        threshold = severity_rank(severity)
        items = [item for item in items if severity_rank(item.severity) >= threshold]
    if json_output:
        console.print_json(json.dumps([item.model_dump(mode="json") for item in items]))
        return
    ConsoleReporter(console).findings_list(items)


@app.command()
def claims(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Show the claim graph from the latest run."""
    _, result = _load_latest(path.resolve())
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "claims": [claim.model_dump(mode="json") for claim in result.claims],
                    "coverage": result.coverage.model_dump(mode="json"),
                }
            )
        )
        return
    ConsoleReporter(console).claims_list(result)


@app.command()
def gate(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Print the gate status of the latest run and exit with its code."""
    _, result = _load_latest(path.resolve())
    if json_output:
        console.print_json(json.dumps(result.gate.model_dump(mode="json")))
    else:
        typer.echo(result.gate.status)
    raise typer.Exit(result.gate.exit_code)


@app.command("repair-plan")
def repair_plan(
    path: Annotated[Path, typer.Argument(help="Artifact path.")] = Path("."),
    profile: Annotated[str | None, typer.Option(help="Profile name to use.")] = None,
    config: Annotated[Path | None, typer.Option(help="Path to veritas.yaml.")] = None,
    evaluate_first: Annotated[
        bool,
        typer.Option(
            "--evaluate/--no-evaluate",
            help="Evaluate now, or plan from the latest stored run.",
        ),
    ] = True,
    json_output: Annotated[bool, typer.Option("--json", help="Emit the plan as JSON.")] = False,
) -> None:
    """Assist mode: evaluate and write repair-plan.json without changing anything."""
    target = path.resolve()
    loaded_config, loaded_profile = _prepare(target, profile, config)

    if evaluate_first:
        artifact = _artifact_for(target, loaded_config, loaded_profile)
        reporter = ConsoleReporter(console, quiet=True)
        engine = Engine(loaded_config, loaded_profile, EngineOptions(progress=reporter.progress))
        try:
            result = asyncio.run(engine.run(artifact))
        except (ConfigError, ProviderError) as exc:
            raise _fail(str(exc)) from exc
        run_dir = unique_run_dir(loaded_config.runs_dir(), result.manifest.run_id)
        write_run(run_dir, result, render_markdown(result))
    else:
        _, result = _load_latest(target)

    plan = RepairPlanner(loaded_config.repair.permissions).plan(result, iteration=1)
    destination = loaded_config.root / ".veritas" / "repair-plan.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(plan.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )

    if json_output:
        console.print_json(json.dumps(plan.model_dump(mode="json")))
    else:
        LoopReporter(console).plan_preview(plan)
        console.print()
        console.print(f"[dim]written: {destination}[/dim]")
        console.print("[dim]Assist mode changes nothing. Use `veritas loop` to apply.[/dim]")


@app.command()
def loop(
    path: Annotated[Path, typer.Argument(help="Artifact path.")] = Path("."),
    profile: Annotated[str | None, typer.Option(help="Profile name to use.")] = None,
    config: Annotated[Path | None, typer.Option(help="Path to veritas.yaml.")] = None,
    max_iterations: Annotated[
        int | None, typer.Option("--max-iterations", help="Override the iteration budget.")
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Evaluate and plan, then stop without changes.")
    ] = False,
    resume: Annotated[
        bool, typer.Option("--resume", help="Continue from the previous loop's ledger.")
    ] = False,
    workspace_mode: Annotated[
        str | None,
        typer.Option("--workspace", help="current | copy | snapshot | worktree."),
    ] = None,
    agent: Annotated[
        str | None, typer.Option("--agent", help="Repair agent provider override.")
    ] = None,
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Print only the stop reason.")
    ] = False,
) -> None:
    """Autopilot: the bounded evaluate, repair and re-evaluate loop."""
    target = path.resolve()
    loaded_config, loaded_profile = _prepare(target, profile, config)

    if not loaded_config.loop.enabled:
        raise _fail("loop.enabled is false in this project's configuration")
    if agent:
        loaded_config.repair.agent.provider = agent

    artifact = _artifact_for(target, loaded_config, loaded_profile)
    reporter = LoopReporter(console, quiet=quiet)
    budget = max_iterations or loaded_config.loop.max_iterations
    reporter.header(loaded_profile.name, str(target), "dry-run" if dry_run else "autopilot", budget)

    mode = workspace_mode or loaded_config.workspace.resolved_mode()
    if dry_run:
        # A dry run must not be able to touch the artifact, whatever it plans.
        mode = "current"

    store = LoopStore(
        loaded_config.loops_dir(),
        _resume_loop_id(loaded_config) if resume else None,
    )
    workspace = open_workspace(
        target,
        mode,
        loop_id=store.loop_id,
        base_dir=loaded_config.workspaces_dir(),
    )

    # The engine evaluates whatever is in the workspace, so repairs made there
    # are what the next evaluation sees.
    workspace_artifact = _rebase_artifact(artifact, workspace.root)
    workspace_config = loaded_config.model_copy()
    workspace_config.root = workspace.root

    try:
        agent_impl = build_repair_agent(loaded_config.repair)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc

    orchestrator = LoopOrchestrator(
        Engine(workspace_config, loaded_profile, EngineOptions(progress=lambda *_: None)),
        RepairPlanner(loaded_config.repair.permissions),
        agent_impl,
        loaded_config.loop,
        store,
        LoopOptions(
            max_iterations=max_iterations,
            dry_run=dry_run,
            resume=resume,
            progress=reporter.progress,
        ),
    )

    try:
        result = asyncio.run(orchestrator.run(workspace_artifact, workspace))
    except (ConfigError, ProviderError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        pass

    report = render_loop_report(result)
    store.write_result(result, report, workspace)
    reporter.summary(result)
    if not quiet:
        console.print(f"[dim]loop: {store.root}[/dim]")

    raise typer.Exit(result.exit_code)


@app.command("loop-report")
def loop_report(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
    raw: Annotated[bool, typer.Option("--raw", help="Print the raw Markdown.")] = False,
) -> None:
    """Show the report from the latest loop."""
    loop_dir = _latest_loop(path.resolve())
    markdown = (loop_dir / "loop-report.md").read_text(encoding="utf-8")
    if raw:
        typer.echo(markdown)
        return
    from rich.markdown import Markdown

    console.print(Markdown(markdown))


@app.command()
def diff(
    first: Annotated[str, typer.Argument(help="Earlier run id, iteration number, or path.")],
    second: Annotated[str, typer.Argument(help="Later run id, iteration number, or path.")],
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Compare two evaluations issue by issue."""
    root = path.resolve()
    try:
        previous = _resolve_evaluation(root, first)
        current = _resolve_evaluation(root, second)
    except FileNotFoundError as exc:
        raise _fail(str(exc)) from exc

    delta = compare_evaluations(previous, current)
    if json_output:
        console.print_json(json.dumps(delta.model_dump(mode="json")))
        return
    LoopReporter(console).diff(delta)


# ----------------------------------------------------------------- helpers


def _prepare(target: Path, profile: str | None, config: Path | None) -> tuple[Any, Profile]:
    """Load configuration and the profile for a target path."""
    config_path = config.resolve() if config else find_config(target)
    try:
        loaded_config = load_config(
            config_path, root=target if config_path is None else config_path.parent
        )
        loaded_profile = _resolve_profile(
            profile or loaded_config.profile,
            loaded_config.root,
            loaded_config.profile_paths,
            None,
        )
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    return loaded_config, loaded_profile


def _artifact_for(target: Path, config: Any, profile: Profile) -> Artifact:
    paths = config.artifact.paths or profile.definition.default_paths
    if target.is_file():
        return Artifact(
            id=target.name,
            type=config.artifact.type or profile.definition.artifact_type,
            root=target.parent,
            paths=[target.name],
        )
    return Artifact(
        id=target.name or str(target),
        type=config.artifact.type or profile.definition.artifact_type,
        root=target,
        paths=paths,
    )


def _rebase_artifact(artifact: Artifact, root: Path) -> Artifact:
    """Point the same artifact definition at a workspace copy."""
    return Artifact(
        id=artifact.id,
        type=artifact.type,
        root=root,
        paths=list(artifact.paths),
        metadata=dict(artifact.metadata),
    )


def _latest_loop(root: Path) -> Path:
    config_path = find_config(root)
    config = load_config(config_path, root=root if config_path is None else config_path.parent)
    loop_dir = latest_loop_dir(config.loops_dir())
    if loop_dir is None:
        raise _fail(f"no loops found under {config.loops_dir()}. Run `veritas loop` first.")
    return loop_dir


def _resume_loop_id(config: Any) -> str | None:
    loop_dir = latest_loop_dir(config.loops_dir())
    return loop_dir.name if loop_dir is not None else None


def _resolve_evaluation(root: Path, reference: str) -> EvaluationResult:
    """Resolve a run id, an iteration number, or a path to an evaluation."""
    config_path = find_config(root)
    config = load_config(config_path, root=root if config_path is None else config_path.parent)

    candidate = Path(reference)
    if candidate.is_dir():
        return load_run(candidate)

    runs = config.runs_dir()
    if (runs / reference).is_dir():
        return load_run(runs / reference)

    if reference.isdigit():
        loop_dir = latest_loop_dir(config.loops_dir())
        if loop_dir is not None:
            iteration = loop_dir / f"iteration-{int(reference):03d}" / "evaluation"
            if iteration.is_dir():
                return load_run(iteration)

    raise FileNotFoundError(
        f"could not resolve '{reference}' to an evaluation. Pass a run id from "
        f"{runs}, an iteration number from the latest loop, or a directory path."
    )


if __name__ == "__main__":  # pragma: no cover
    app()
