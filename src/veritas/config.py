"""Configuration loading.

Two layers: ``veritas.yaml`` in the project (what to evaluate, with which
models and policy) and the profile directory (which judges and checks exist,
and the prompts behind them). Secrets are never configuration values; only the
*names* of environment variables appear in YAML.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CONFIG_FILENAME = "veritas.yaml"
RUNS_DIRNAME = ".veritas"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    """Raised for malformed or incomplete configuration."""


class ModelConfig(BaseModel):
    """Model settings for one judge role."""

    model_config = ConfigDict(extra="allow")

    provider: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout: float = 120.0
    max_retries: int = 3
    base_url: str | None = None
    api_key_env: str | None = None


class GatePolicy(BaseModel):
    """Deterministic gate thresholds."""

    model_config = ConfigDict(extra="forbid")

    fail_on: list[str] = Field(default_factory=lambda: ["critical"])
    revise_on: list[str] = Field(default_factory=list)
    max_critical: int = 0
    max_major: int = 0
    max_minor: int | None = None
    require_checks: list[str] = Field(default_factory=list)
    min_evidence_coverage: float | None = None
    warn_on_minor: bool = True


class CheckConfig(BaseModel):
    """One deterministic check instance."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    type: str = "command"
    command: list[str] = Field(default_factory=list)
    working_dir: str | None = None
    timeout: float = 600.0
    severity_on_failure: Literal["info", "minor", "major", "critical"] = "major"
    required_paths: list[str] = Field(default_factory=list)
    required_sections: list[str] = Field(default_factory=list)
    target: str | None = None


class ExecutionConfig(BaseModel):
    """Allow-list for anything that runs a subprocess."""

    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    env_passthrough: list[str] = Field(default_factory=list)


class ArtifactConfig(BaseModel):
    """Which paths make up the artifact."""

    model_config = ConfigDict(extra="allow")

    type: str = "document"
    paths: list[str] = Field(default_factory=list)


class VeritasConfig(BaseModel):
    """The parsed ``veritas.yaml``."""

    model_config = ConfigDict(extra="allow")

    version: int = 1
    profile: str = "generic-document"
    profile_paths: list[str] = Field(default_factory=list)
    artifact: ArtifactConfig = Field(default_factory=ArtifactConfig)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    judges: list[str] | None = None
    gate: GatePolicy | None = None
    checks: dict[str, CheckConfig] = Field(default_factory=dict)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    concurrency: int = 4
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    def runs_dir(self) -> Path:
        return self.root / RUNS_DIRNAME / "runs"


def interpolate_env(value: Any, *, missing: list[str] | None = None) -> Any:
    """Expand ``${VAR}`` and ``${VAR:-default}`` references inside config values."""

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        found = os.environ.get(name)
        if found is None or found == "":
            if default is not None:
                return default
            if missing is not None:
                missing.append(name)
            return ""
        return found

    if isinstance(value, str):
        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: interpolate_env(item, missing=missing) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate_env(item, missing=missing) for item in value]
    return value


def load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML mapping, with env interpolation applied to every value."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    missing: list[str] = []
    resolved = interpolate_env(raw, missing=missing)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ConfigError(
            f"{path} references unset environment variables: {names}. "
            "Copy .env.example and export the values, or use ${VAR:-default}."
        )
    return resolved  # type: ignore[no-any-return]


def find_config(start: Path) -> Path | None:
    """Walk upward from ``start`` looking for ``veritas.yaml``."""
    current = start.resolve()
    for candidate in [current, *current.parents]:
        config = candidate / CONFIG_FILENAME
        if config.is_file():
            return config
    return None


def load_config(path: Path | None = None, *, root: Path | None = None) -> VeritasConfig:
    """Load configuration, falling back to defaults when no file exists."""
    if path is None:
        data: dict[str, Any] = {}
        base = (root or Path.cwd()).resolve()
    else:
        data = load_yaml(path)
        base = (root or path.parent).resolve()
    data.setdefault("root", base)
    try:
        config = VeritasConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"invalid configuration: {exc}") from exc
    config.root = base
    return config
