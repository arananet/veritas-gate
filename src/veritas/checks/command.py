"""Command check: run an allow-listed process against the artifact.

Nothing executes unless the executable appears in ``execution.allow``. There is
no implicit allow-list, no shell, and no inherited environment beyond what
``execution.env_passthrough`` names.
"""

from __future__ import annotations

import asyncio
import os
import time

from veritas.artifacts.base import Artifact
from veritas.checks.base import check_finding
from veritas.config import CheckConfig, ExecutionConfig
from veritas.models.evaluation import CheckResult

OUTPUT_LIMIT = 8000
ALWAYS_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


class CommandNotAllowedError(RuntimeError):
    """Raised when a configured command is absent from the execution allow-list."""


class CommandCheck:
    """Execute a configured command and translate its exit status into findings."""

    def __init__(self, name: str, config: CheckConfig, execution: ExecutionConfig) -> None:
        self.name = name
        self.config = config
        self.execution = execution

    def _validate(self) -> str:
        if not self.config.command:
            raise CommandNotAllowedError(f"check '{self.name}' defines no command")
        executable = self.config.command[0]
        allowed = set(self.execution.allow)
        if executable not in allowed and os.path.basename(executable) not in allowed:
            raise CommandNotAllowedError(
                f"command '{executable}' is not in execution.allow "
                f"({sorted(allowed) or 'empty'}); add it to veritas.yaml to permit execution"
            )
        return executable

    def _environment(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for name in (*ALWAYS_PASSTHROUGH, *self.execution.env_passthrough):
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        return env

    async def run(self, artifact: Artifact) -> CheckResult:
        started = time.monotonic()
        try:
            self._validate()
        except CommandNotAllowedError as exc:
            return CheckResult(
                check=self.name,
                status="error",
                summary=str(exc),
                findings=[
                    check_finding(
                        self.name,
                        1,
                        title=f"Check '{self.name}' was not executed",
                        severity="info",
                        description=str(exc),
                        recommendation="Add the command to execution.allow if you trust it.",
                    )
                ],
                duration_seconds=round(time.monotonic() - started, 3),
            )

        workdir = artifact.root
        if self.config.working_dir:
            workdir = (artifact.root / self.config.working_dir).resolve()

        try:
            process = await asyncio.create_subprocess_exec(
                *self.config.command,
                cwd=str(workdir),
                env=self._environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=self.config.timeout)
            returncode = process.returncode or 0
            output = stdout.decode("utf-8", errors="replace")
        except TimeoutError:
            process.kill()
            return self._failure(
                started,
                f"timed out after {self.config.timeout:.0f}s",
                ["The command did not finish within the configured timeout."],
            )
        except (OSError, ValueError) as exc:
            return self._failure(started, f"could not start: {exc}", [str(exc)])

        duration = round(time.monotonic() - started, 3)
        tail = output[-OUTPUT_LIMIT:]
        if returncode == 0:
            return CheckResult(
                check=self.name,
                status="pass",
                summary=f"`{' '.join(self.config.command)}` succeeded",
                duration_seconds=duration,
                metadata={"exit_code": 0, "output": tail},
            )
        return CheckResult(
            check=self.name,
            status="fail",
            summary=f"`{' '.join(self.config.command)}` exited with {returncode}",
            findings=[
                check_finding(
                    self.name,
                    1,
                    title=f"Check '{self.name}' failed",
                    severity=self.config.severity_on_failure,
                    description=f"Command exited with status {returncode}.",
                    evidence=[line for line in tail.strip().splitlines()[-20:] if line.strip()],
                    recommendation="Fix the underlying failure and re-run the evaluation.",
                )
            ],
            duration_seconds=duration,
            metadata={"exit_code": returncode, "output": tail},
        )

    def _failure(self, started: float, reason: str, evidence: list[str]) -> CheckResult:
        return CheckResult(
            check=self.name,
            status="fail",
            summary=f"`{' '.join(self.config.command)}` {reason}",
            findings=[
                check_finding(
                    self.name,
                    1,
                    title=f"Check '{self.name}' failed",
                    severity=self.config.severity_on_failure,
                    description=reason,
                    evidence=evidence,
                )
            ],
            duration_seconds=round(time.monotonic() - started, 3),
        )
