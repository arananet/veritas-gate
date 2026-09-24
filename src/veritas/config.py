"""Configuration loading.

Two layers: ``veritas.yaml`` in the project (what to evaluate, with which
models and policy) and the profile directory (which judges and checks exist,
and the prompts behind them). Secrets are never configuration values; only the
*names* of environment variables appear in YAML.
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Unset by default, and omitted from the request when unset: `temperature`
    # is deprecated on current Claude models and returns a 400 there. Set it
    # explicitly for a provider that still accepts it.
    temperature: float | None = None
    max_tokens: int = 16000
    timeout: float = 120.0
    max_retries: int = 3
    base_url: str | None = None
    api_key_env: str | None = None
    # How this provider's TLS certificate is verified:
    #   true          — the certificates bundled with Python (the default)
    #   "truststore"  — the operating system trust store (macOS Keychain,
    #                   Windows cert store, the system CA set on Linux). Use this
    #                   behind a corporate proxy that intercepts TLS: the CA is
    #                   already installed there. Requires the `truststore` extra.
    #   "/path/ca.pem" — an explicit CA bundle
    #   false         — no verification at all. Anyone on the network path can
    #                   read your API key and the artifact you are evaluating.
    #                   For a local endpoint with a self-signed certificate only.
    tls_verify: bool | str = True


class AcceptedRisk(BaseModel):
    """A finding the maintainer has decided to carry, on the record.

    Accepting a risk stops it blocking the gate. It never hides it: the finding
    is still reported, still counted, and still named alongside the reason it
    was accepted. An exception nobody can see is indistinguishable from a bug.
    """

    model_config = ConfigDict(extra="forbid")

    # Match by exact identity, or by where and what kind of problem it is.
    # The id is precise but brittle: it is derived from the finding's title, so
    # a judge rewording the same problem after the artifact changes produces a
    # new id. category/location survive that, at the cost of being broader.
    id: str | None = None
    category: str | None = None
    location: str | None = None

    reason: str
    expires: date | None = None

    @field_validator("reason")
    @classmethod
    def _reason_must_say_something(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(
                "an accepted risk needs a reason: it is the only record of why "
                "a known problem was allowed through"
            )
        return value

    @model_validator(mode="after")
    def _must_match_something(self) -> AcceptedRisk:
        if not (self.id or self.category or self.location):
            raise ValueError(
                "an accepted risk needs an id, a category or a location to match; "
                "one with no criteria would accept every finding"
            )
        return self

    @property
    def label(self) -> str:
        """How this rule is named in reports."""
        if self.id:
            return self.id
        parts = []
        if self.category:
            parts.append(f"category={self.category}")
        if self.location:
            parts.append(f"location={self.location}")
        return " ".join(parts)

    def active_on(self, today: date) -> bool:
        return self.expires is None or self.expires >= today

    def matches(self, finding_category: str, finding_location: str | None, key: str) -> bool:
        """Whether this rule covers a finding. Every criterion given must hold."""
        if self.id and key != self.id:
            return False
        if self.category:
            wanted = self.category.strip().lower()
            actual = finding_category.strip().lower()
            # A prefix match lets `category: check` cover `check/tests`.
            if actual != wanted and not actual.startswith(f"{wanted}/"):
                return False
        # Locations are prose citing several places, so this is a substring
        # test rather than equality.
        return not self.location or (
            self.location.strip().lower() in (finding_location or "").lower()
        )


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
    # A judge that could not run did not approve anything. An evaluation that
    # failed to happen must never be reported as a pass.
    fail_on_judge_error: bool = True
    # Findings carried deliberately. They are reported and counted as usual;
    # they simply do not block. Each needs a reason, and may carry an expiry
    # after which it blocks again.
    accepted_risks: list[AcceptedRisk] = Field(default_factory=list)


class ContentPattern(BaseModel):
    """A regular expression the artifact's text is checked against.

    ``description`` is what a reader is told when it fails, so it should say
    what was expected rather than restate the expression.
    """

    model_config = ConfigDict(extra="forbid")

    pattern: str
    description: str = ""

    @field_validator("pattern")
    @classmethod
    def _must_compile(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid regular expression {value!r}: {exc}") from exc
        return value

    def label(self) -> str:
        return self.description.strip() or self.pattern


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
    # For the content-patterns check: regular expressions that must appear in
    # the target, and ones that must not. A citation to a mutable branch is as
    # much a defect as a missing DOI, so both directions are needed.
    required_patterns: list[ContentPattern] = Field(default_factory=list)
    forbidden_patterns: list[ContentPattern] = Field(default_factory=list)
    case_sensitive: bool = False
    # For reference-integrity: a cited path that exists but sits outside
    # artifact.paths is a statement about configuration, not a defect in the
    # artifact. Excluding a lockfile or a PDF is how an evaluation is kept
    # affordable, so this is advisory by default.
    severity_on_unsupplied: Literal["info", "minor", "major", "critical"] = "minor"


class ExecutionConfig(BaseModel):
    """Allow-list for anything that runs a subprocess."""

    model_config = ConfigDict(extra="forbid")

    allow: list[str] = Field(default_factory=list)
    env_passthrough: list[str] = Field(default_factory=list)


# ── Repair configuration ─────────────────────────────────────────────────────
# These live here rather than in veritas.repair because they are configuration:
# keeping them in config.py is also what keeps the config and repair packages
# free of a circular import.

# Which permission key governs which action type.
ACTION_PERMISSION: dict[str, str] = {
    "artifact_edit": "documentation",
    "documentation": "documentation",
    "code_change": "source_code",
    "test_change": "tests",
    "experiment": "experiments",
    "dataset_change": "datasets",
    "claim_change": "scientific_claims",
    "human_decision": "human_decision",
}


class RepairPermissions(BaseModel):
    """What the repair agent is allowed to do, by category.

    The defaults are deliberate: representation-level work is autonomous;
    anything that would create or alter evidence, methodology or a scientific
    claim is not.
    """

    model_config = ConfigDict(extra="allow")

    documentation: bool = True
    source_code: bool = True
    tests: bool = True
    experiments: bool = False
    datasets: bool = False
    scientific_claims: bool = False
    methodology: bool = False
    human_decision: bool = False

    edit_files: bool = True
    run_tests: bool = False
    run_build: bool = False

    def allows(self, action_type: str) -> bool:
        key = ACTION_PERMISSION.get(action_type)
        if key is None:
            return False
        return bool(getattr(self, key, False))

    def reason_for(self, action_type: str) -> str:
        key = ACTION_PERMISSION.get(action_type, action_type)
        return (
            f"action type '{action_type}' requires repair.permissions.{key}, "
            "which is disabled. Enable it explicitly, or resolve this finding by hand."
        )


class RepairAgentConfig(BaseModel):
    """How to reach the configured repair agent."""

    model_config = ConfigDict(extra="allow")

    provider: str = "mock"
    command: list[str] = Field(default_factory=list)
    timeout: float = 1800.0
    working_dir: str | None = None
    env_passthrough: list[str] = Field(default_factory=list)
    prompt_version: str = "1"


class RepairConfig(BaseModel):
    """The ``repair:`` block of veritas.yaml."""

    model_config = ConfigDict(extra="allow")

    mode: str = "autopilot"
    agent: RepairAgentConfig = Field(default_factory=RepairAgentConfig)
    permissions: RepairPermissions = Field(default_factory=RepairPermissions)
    workspace: str = "current"  # current | copy | worktree


class LoopConfig(BaseModel):
    """The ``loop:`` block: how the bounded repair loop may run.

    Every field here exists to make the loop stop. There is no configuration
    that produces an unbounded loop.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_iterations: int = 5
    consecutive_non_improving_iterations: int = 2
    same_blocker_repeated: int = 2
    stop_on_new_critical: bool = True
    stop_on_regression: bool = True
    accept_warnings: bool = True
    # What to do when a finding needs a human decision:
    #   "defer" — repair the mechanical findings first, then stop and report the
    #             decisions. Each pass leaves less noise, and the human is asked
    #             against a cleaner artifact.
    #   "stop"  — stop immediately, repairing nothing. For an unattended run
    #             where halting at the first human decision is the point.
    # Neither mode ever dispatches a human-blocked action to a repair agent.
    on_human_decision: Literal["defer", "stop"] = "defer"
    max_cost_usd: float | None = None
    max_changed_files: int | None = 25


class WorkspaceConfig(BaseModel):
    """Where repairs happen, and what counts as part of the artifact."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["current", "copy", "snapshot", "worktree"] = "worktree"
    include_untracked: bool = True
    respect_gitignore: bool = True
    respect_veritasignore: bool = True

    def resolved_mode(self) -> str:
        """``snapshot`` is a copy of the tree as it stands, uncommitted work included."""
        return "copy" if self.mode == "snapshot" else self.mode


class ArtifactConfig(BaseModel):
    """Which paths make up the artifact."""

    model_config = ConfigDict(extra="allow")

    type: str = "document"
    paths: list[str] = Field(default_factory=list)
    # Per-file character cap on what reaches a judge. Raise it for a long
    # manuscript; lower it only to control cost on a large tree.
    max_file_chars: int = 400_000


class ModelPrice(BaseModel):
    """What one model charges, per million tokens.

    Operator-supplied. Veritas ships no rates: prices change without notice and
    differ by provider and tier, and a stale rate frozen into an immutable run
    record is worse than no rate at all.
    """

    model_config = ConfigDict(extra="forbid")

    input_per_million: float = 0.0
    output_per_million: float = 0.0

    @field_validator("input_per_million", "output_per_million")
    @classmethod
    def _must_not_be_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("a price per million tokens cannot be negative")
        return value

    def cost_for(self, input_tokens: int, output_tokens: int) -> float:
        return round(
            input_tokens / 1_000_000 * self.input_per_million
            + output_tokens / 1_000_000 * self.output_per_million,
            6,
        )


class PricingConfig(BaseModel):
    """An optional price list, keyed by the model identifier the provider reports."""

    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    rates: dict[str, ModelPrice] = Field(default_factory=dict)


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
    loop: LoopConfig = Field(default_factory=LoopConfig)
    repair: RepairConfig = Field(default_factory=RepairConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    # Per-judge scope. Every judge reading the whole artifact cost around
    # 300,000 input tokens per judge on a real paper. A judge named here reads
    # only the artifact segments under these paths; one not named reads it all.
    # Narrowing is deliberate: a judge that cannot see the code cannot report
    # that the code contradicts the paper.
    judge_paths: dict[str, list[str]] = Field(default_factory=dict)
    concurrency: int = 4
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    def runs_dir(self) -> Path:
        return self.root / RUNS_DIRNAME / "runs"

    def loops_dir(self) -> Path:
        return self.root / RUNS_DIRNAME / "loops"

    def workspaces_dir(self) -> Path:
        return self.root / RUNS_DIRNAME / "workspaces"


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
