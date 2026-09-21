"""Deterministic checks. Execution is opt-in through an allow-list."""

from __future__ import annotations

from veritas.checks.base import Check, check_finding
from veritas.checks.command import CommandCheck, CommandNotAllowedError
from veritas.checks.structure import RequiredPathsCheck, RequiredSectionsCheck
from veritas.config import CheckConfig, ConfigError, ExecutionConfig

CHECK_TYPES = ("command", "required-paths", "required-sections")

__all__ = [
    "CHECK_TYPES",
    "Check",
    "CommandCheck",
    "CommandNotAllowedError",
    "RequiredPathsCheck",
    "RequiredSectionsCheck",
    "build_check",
    "check_finding",
]


def build_check(name: str, config: CheckConfig, execution: ExecutionConfig) -> Check:
    """Instantiate the check implementation named by ``config.type``."""
    if config.type == "command":
        return CommandCheck(name, config, execution)
    if config.type == "required-paths":
        return RequiredPathsCheck(name, config)
    if config.type == "required-sections":
        return RequiredSectionsCheck(name, config)
    raise ConfigError(
        f"check '{name}' has unknown type '{config.type}'; known types: {', '.join(CHECK_TYPES)}"
    )
