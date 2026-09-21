"""The ``veritas`` command line.

Exit codes are meaningful so CI can gate on them:
``0`` pass, ``1`` pass with warnings, ``2`` revise, ``3`` fail, ``4`` usage or
configuration error.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from veritas import __version__
from veritas.artifacts.base import Artifact
from veritas.config import CONFIG_FILENAME, ConfigError, find_config, load_config
from veritas.engine import Engine, EngineOptions
from veritas.models.evaluation import EvaluationResult
from veritas.models.finding import severity_rank
from veritas.profiles import Profile, available_profiles, load_profile, load_profile_dir
from veritas.providers.base import ProviderError
from veritas.reports import ConsoleReporter, render_markdown
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
    return f"""\
version: 1

profile: {profile}

artifact:
  paths:
    - .

# Secrets never live here. Only environment variable names and model ids do.
models:
  default:
    provider: anthropic
    model: ${{VERITAS_DEFAULT_MODEL}}
  adversarial:
    provider: openai
    model: ${{VERITAS_ADVERSARIAL_MODEL}}
  meta:
    provider: google
    model: ${{VERITAS_META_MODEL}}

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
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path("."),
) -> None:
    """Print the advisory repair plan. Veritas never applies it."""
    run_dir, _ = _load_latest(path.resolve())
    typer.echo((run_dir / "repair-plan.json").read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    app()
