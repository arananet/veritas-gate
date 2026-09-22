"""Repair: planning, permissions, workspaces and agents.

The repairer is deliberately separate from the evaluator. It applies authorized
changes and reports what it did; it never decides whether its own work was
correct.
"""

from __future__ import annotations

from collections.abc import Callable

from veritas.repair.base import PermissionViolation, RepairAgent, enforce_permissions
from veritas.repair.cli_agent import GenericCLIRepairAgent, load_repair_prompt
from veritas.repair.mock import MockRepairAgent
from veritas.repair.permissions import RepairAgentConfig, RepairConfig, RepairPermissions
from veritas.repair.planner import RepairPlanner
from veritas.repair.workspace import Workspace, open_workspace, temporary_workspace

__all__ = [
    "GenericCLIRepairAgent",
    "MockRepairAgent",
    "PermissionViolation",
    "RepairAgent",
    "RepairAgentConfig",
    "RepairConfig",
    "RepairPermissions",
    "RepairPlanner",
    "Workspace",
    "build_repair_agent",
    "enforce_permissions",
    "load_repair_prompt",
    "open_workspace",
    "temporary_workspace",
]


def build_repair_agent(
    config: RepairConfig, on_output: Callable[[str], None] | None = None
) -> RepairAgent:
    """Instantiate the configured agent.

    New agents (Codex, Claude Code, an API-backed repairer, a human queue) plug
    in here; the orchestrator is never coupled to any of them.
    """
    from veritas.config import ConfigError

    provider = config.agent.provider
    if provider == "mock":
        return MockRepairAgent(permissions=config.permissions)
    if provider in ("generic-cli", "cli"):
        return GenericCLIRepairAgent(config.agent, config.permissions, on_output=on_output)
    raise ConfigError(
        f"unknown repair agent provider '{provider}'; available providers: generic-cli, mock"
    )
