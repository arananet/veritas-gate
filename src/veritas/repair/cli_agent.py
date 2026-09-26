"""GenericCLIRepairAgent: drive any CLI coding agent as a worker.

The command is a template, so the same adapter drives whichever tool you have:

```yaml
repair:
  agent:
    provider: generic-cli
    command:
      - sh
      - -c
      - 'codex exec --approve-for-me -C {workspace} "$(cat {prompt_file})"'
```

```yaml
repair:
  agent:
    provider: generic-cli
    command:
      - sh
      - -c
      - 'claude -p "$(cat {prompt_file})" --permission-mode acceptEdits'
```

Two things a command must get right, because both fail silently otherwise: the
prompt is passed as *text*, not as a path, and the agent must be allowed to
write files without waiting for an approval nobody is there to give. An agent
that runs and changes nothing is reported as a failure, not as a partial.

Nothing about a specific vendor is encoded here, so swapping the tool is a
configuration change and the rest of Veritas is unaffected. A vendor-specific
agent (Codex, Claude Code, an API-backed one, a human queue) can be added later
as another implementation of the same protocol without touching the orchestrator.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

from veritas.artifacts.base import Artifact
from veritas.models.repair import AppliedChange, RepairPlan, RepairResult
from veritas.repair.base import enforce_permissions
from veritas.repair.permissions import RepairAgentConfig, RepairPermissions
from veritas.repair.workspace import Workspace

ALWAYS_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
OUTPUT_LIMIT = 16_000


class GenericCLIRepairAgent:
    """Invoke a configured CLI agent with a generated prompt and plan."""

    name = "generic-cli"

    def __init__(
        self,
        config: RepairAgentConfig,
        permissions: RepairPermissions | None = None,
        *,
        prompt: str | None = None,
        on_output: Callable[[str], None] | None = None,
        thesis: list[str] | None = None,
        frozen: list[str] | None = None,
    ) -> None:
        self.frozen = list(frozen or [])
        self.config = config
        self.permissions = permissions or RepairPermissions()
        self.prompt_template = prompt if prompt is not None else load_repair_prompt()
        # Called with each line the agent writes, as it writes it. Whatever the
        # configured tool prints; Veritas neither parses nor interprets it.
        self.on_output = on_output
        self.thesis = list(thesis or [])

    async def repair(
        self,
        artifact: Artifact,
        plan: RepairPlan,
        workspace: Workspace,
    ) -> RepairResult:
        enforce_permissions(plan, self.permissions)

        if not self.config.command:
            return _failed(plan, "repair.agent.command is not configured")

        tmp = workspace.root / ".veritas" / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        plan_file = tmp / f"repair-plan-{plan.iteration:03d}.json"
        prompt_file = tmp / f"repair-prompt-{plan.iteration:03d}.md"
        result_file = tmp / f"repair-result-{plan.iteration:03d}.json"
        result_file.unlink(missing_ok=True)

        plan_file.write_text(
            json.dumps(plan.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )
        prompt_file.write_text(self.render_prompt(plan, plan_file, result_file), encoding="utf-8")

        command = [
            _template(part, plan_file, prompt_file, result_file, workspace)
            for part in self.config.command
        ]
        before = workspace.snapshot()

        lines: list[str] = []
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace.root),
                env=self._environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except (OSError, ValueError) as exc:
            return _failed(plan, f"the repair command could not be started: {exc}")

        try:
            # Read line by line rather than waiting for the process to exit: a
            # repair runs for minutes, and a spinner alone cannot be told apart
            # from a hang. Nothing here parses the output -- it belongs to
            # whichever CLI agent is configured.
            await asyncio.wait_for(self._stream(process, lines), timeout=self.config.timeout)
            returncode = process.returncode or 0
        except TimeoutError:
            process.kill()
            return _failed(
                plan,
                f"the repair command timed out after {self.config.timeout:.0f}s",
                output="\n".join(lines)[-OUTPUT_LIMIT:],
            )
        output = "\n".join(lines)[-OUTPUT_LIMIT:]

        changed = workspace.changed_since(before)
        changed = [path for path in changed if not path.startswith(".veritas/")]
        reported = _read_result(result_file)

        # An agent handed actions that changed no file did not repair anything,
        # whatever it exited with or claims in its own report. A misconfigured
        # command — a prompt passed as a path, a sandbox that forbids writing,
        # an approval nobody will give — exits zero and does nothing, and that
        # used to be recorded as a quiet `partial` the loop carried straight past.
        no_op = bool(plan.actions) and not changed

        if reported is not None:
            # Trust the agent's report for narrative fields, but the changed-file
            # list comes from the filesystem, not from the agent's claim.
            reported.changes = _merge_changes(reported.changes, changed)
            reported.metadata = {
                **reported.metadata,
                "agent": self.name,
                "command": command,
                "exit_code": returncode,
                "output": output,
            }
            if no_op:
                reported.status = "failed"
                reported.notes = [*reported.notes, _no_op_note(command, returncode)]
            return reported

        if no_op:
            status = "failed"
            notes = [_no_op_note(command, returncode)]
        else:
            status = "completed" if returncode == 0 else "failed"
            notes = [f"the agent wrote no result file; status inferred from exit code {returncode}"]
        return RepairResult(
            action_ids=[action.id for action in plan.actions],
            status=status,  # type: ignore[arg-type]
            changes=[
                AppliedChange(file=path, description="changed by the repair agent")
                for path in changed
            ],
            notes=notes,
            metadata={
                "agent": self.name,
                "command": command,
                "exit_code": returncode,
                "output": output,
            },
        )

    async def _stream(self, process: asyncio.subprocess.Process, lines: list[str]) -> None:
        """Collect the agent's output, handing each line to the observer as it lands."""
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            # Lenient decoding: an agent may emit progress bytes that are not
            # valid UTF-8 mid-line, and that must not end the repair.
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            lines.append(line)
            if self.on_output is not None and line.strip():
                self.on_output(line)
        await process.wait()

    def render_prompt(self, plan: RepairPlan, plan_file: Path, result_file: Path) -> str:
        """Build the repair prompt.

        The agent is given the normalized plan, never the judges' raw output,
        their reasoning, or any previous agent's reasoning.
        """
        sections = [
            self.prompt_template.strip(),
            "## Permissions",
            "",
            f"- Edit files: {_yesno(self.permissions.edit_files)}",
            f"- Run tests: {_yesno(self.permissions.run_tests)}",
            f"- Run build: {_yesno(self.permissions.run_build)}",
            f"- Run experiments: {_yesno(self.permissions.experiments)}",
            f"- Modify datasets: {_yesno(self.permissions.datasets)}",
            f"- Modify methodology: {_yesno(self.permissions.methodology)}",
            f"- Alter scientific claims: {_yesno(self.permissions.scientific_claims)}",
            "",
            *self._thesis_lines(),
            *self._frozen_lines(),
            f"## Repair plan (iteration {plan.iteration})",
            "",
            f"The machine-readable plan is at `{plan_file}`.",
            f"Write your result to `{result_file}`.",
            "",
        ]
        for action in plan.actions:
            sections.extend(
                [
                    f"### {action.id} ({action.priority}, {action.action_type})",
                    "",
                    action.instruction.strip(),
                    "",
                    "Allowed files:",
                    *(
                        [f"- `{path}`" for path in action.allowed_files]
                        or ["- (none specified; make the smallest change that resolves this)"]
                    ),
                    "",
                ]
            )
        return "\n".join(sections)

    def _frozen_lines(self) -> list[str]:
        """Evidence the agent must never rewrite, whatever a finding asks."""
        if not self.frozen:
            return []
        return [
            "## Frozen files (never modify)",
            "",
            "These record what an experiment actually produced. Do not edit, move or",
            "delete them, even where a finding says they are wrong. Record a correction",
            "in the manuscript or an errata file instead. Changes here are reverted.",
            "",
            *[f"- `{item}`" for item in self.frozen],
            "",
        ]

    def _thesis_lines(self) -> list[str]:
        """What the work sets out to show: bound it to the evidence, never delete it."""
        if not self.thesis:
            return []
        return [
            "## Claims to preserve",
            "",
            "The work sets out to demonstrate the following. Keep each one. Where a",
            "finding says the evidence does not support it as written, narrow the",
            "wording to what the evidence does support; do not delete the claim, and",
            "do not weaken it further than the evidence requires.",
            "",
            *[f"- {item}" for item in self.thesis],
            "",
        ]

    def _environment(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for name in (*ALWAYS_PASSTHROUGH, *self.config.env_passthrough):
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        return env


def load_repair_prompt() -> str:
    """Load the versioned repair prompt shipped with the package."""
    resource = resources.files("veritas.repair") / "prompts" / "repair.md"
    return resource.read_text(encoding="utf-8")


def _template(
    part: str, plan_file: Path, prompt_file: Path, result_file: Path, workspace: Workspace
) -> str:
    return (
        part.replace("{prompt_file}", str(prompt_file))
        .replace("{repair_prompt_file}", str(prompt_file))
        .replace("{plan_file}", str(plan_file))
        .replace("{result_file}", str(result_file))
        .replace("{workspace}", str(workspace.root))
    )


def _read_result(path: Path) -> RepairResult | None:
    if not path.is_file():
        return None
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return RepairResult.model_validate(payload)
    except Exception:
        return None


def _merge_changes(reported: list[AppliedChange], observed: list[str]) -> list[AppliedChange]:
    """Reconcile what the agent said it changed with what actually changed."""
    described = {change.file: change.description for change in reported}
    return [
        AppliedChange(file=path, description=described.get(path, "changed by the repair agent"))
        for path in observed
    ]


def _yesno(value: bool) -> str:
    return "yes" if value else "NO"


def _failed(plan: RepairPlan, message: str, output: str = "") -> RepairResult:
    metadata: dict[str, Any] = {"agent": GenericCLIRepairAgent.name}
    if output:
        # Keep what the agent managed to say before it was killed; it is
        # usually the only clue to why.
        metadata["output"] = output
    return RepairResult(
        action_ids=[action.id for action in plan.actions],
        status="failed",
        notes=[message],
        metadata=metadata,
    )


def _no_op_note(command: list[str], returncode: int) -> str:
    """Say plainly that nothing happened, and give the operator the cause to check."""
    return (
        f"the repair command exited {returncode} and changed no file, so none of the "
        f"planned actions were carried out. Command: {' '.join(command)}"
    )
