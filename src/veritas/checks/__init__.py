"""Deterministic checks. Execution is opt-in through an allow-list."""

from __future__ import annotations

from veritas.checks.arxiv import ArxivPackageCheck
from veritas.checks.base import Check, check_finding
from veritas.checks.command import CommandCheck, CommandNotAllowedError
from veritas.checks.derived import DerivedFreshnessCheck
from veritas.checks.integrity import FrozenIntegrityCheck
from veritas.checks.numbers import NumericTraceabilityCheck
from veritas.checks.references import ReferenceIntegrityCheck
from veritas.checks.structure import (
    ContentPatternsCheck,
    RequiredPathsCheck,
    RequiredSectionsCheck,
)
from veritas.config import CheckConfig, ConfigError, Derivation, ExecutionConfig

CHECK_TYPES = (
    "command",
    "required-paths",
    "required-sections",
    "content-patterns",
    "reference-integrity",
    "arxiv-package",
    "numeric-traceability",
    "derived-freshness",
    "frozen-integrity",
)

__all__ = [
    "CHECK_TYPES",
    "ArxivPackageCheck",
    "Check",
    "CommandCheck",
    "CommandNotAllowedError",
    "ContentPatternsCheck",
    "NumericTraceabilityCheck",
    "ReferenceIntegrityCheck",
    "RequiredPathsCheck",
    "RequiredSectionsCheck",
    "build_check",
    "check_finding",
]


def build_check(
    name: str,
    config: CheckConfig,
    execution: ExecutionConfig,
    derived: list[Derivation] | None = None,
    frozen: list[str] | None = None,
) -> Check:
    """Instantiate the check implementation named by ``config.type``."""
    if config.type == "command":
        return CommandCheck(name, config, execution)
    if config.type == "required-paths":
        return RequiredPathsCheck(name, config)
    if config.type == "required-sections":
        return RequiredSectionsCheck(name, config)
    if config.type == "content-patterns":
        return ContentPatternsCheck(name, config)
    if config.type == "reference-integrity":
        return ReferenceIntegrityCheck(name, config)
    if config.type == "arxiv-package":
        return ArxivPackageCheck(name, config)
    if config.type == "numeric-traceability":
        return NumericTraceabilityCheck(name, config)
    if config.type == "derived-freshness":
        return DerivedFreshnessCheck(name, config, list(derived or []))
    if config.type == "frozen-integrity":
        return FrozenIntegrityCheck(name, config, list(frozen or []))
    raise ConfigError(
        f"check '{name}' has unknown type '{config.type}'; known types: {', '.join(CHECK_TYPES)}"
    )
