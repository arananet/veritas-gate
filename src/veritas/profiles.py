"""Profile registry.

A profile is a *plugin*, not core logic: a directory holding ``profile.yaml``,
an optional ``rubric.yaml`` and a ``prompts/`` folder. The engine contains no
knowledge of scientific papers, repositories or architecture reviews — adding a
new domain means adding a directory, never touching the orchestration code.

Resolution order:

1. ``--profile-path`` / ``profile_paths:`` entries from configuration
2. ``<project>/profiles/<name>``
3. ``$VERITAS_PROFILE_PATH`` (os.pathsep-separated)
4. profiles bundled with the installed package, or the repository's own
   ``profiles/`` when running from a source checkout
5. ``veritas.profiles`` entry points published by third-party packages
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from veritas.config import CheckConfig, ConfigError, GatePolicy, load_yaml

ENTRY_POINT_GROUP = "veritas.profiles"
ENV_PATH_VAR = "VERITAS_PROFILE_PATH"


class JudgeSpec(BaseModel):
    """Declarative definition of one judge. No Python subclass required."""

    model_config = ConfigDict(extra="allow")

    name: str
    prompt: str | None = None
    model_role: str | None = None
    version: str = "1"
    enabled: bool = True
    extracts_claims: bool = False
    # A visual judge reviews rendered pages of artifact.pdf instead of text.
    visual: bool = False
    description: str = ""


class ScaffoldEntry(BaseModel):
    """A file a profile expects the artifact to have, and how to create it."""

    model_config = ConfigDict(extra="forbid")

    path: str
    description: str = ""
    # Exactly one of: a built-in generator, or a template in the profile's
    # scaffold/ directory with {{dotted.field}} placeholders.
    generator: str | None = None
    template: str | None = None
    requires: list[str] = Field(default_factory=list)


class ProfileDefinition(BaseModel):
    """Parsed ``profile.yaml``."""

    model_config = ConfigDict(extra="allow")

    profile: str
    version: str = "1"
    description: str = ""
    artifact_type: str = "document"
    default_paths: list[str] = Field(default_factory=lambda: ["."])
    judges: list[JudgeSpec] = Field(default_factory=list)
    meta_model_role: str = "meta"
    checks: dict[str, CheckConfig] = Field(default_factory=dict)
    gate: GatePolicy = Field(default_factory=GatePolicy)
    rubric: dict[str, Any] = Field(default_factory=dict)
    scaffold: list[ScaffoldEntry] = Field(default_factory=list)


@dataclass(slots=True)
class Profile:
    """A loaded profile plus the directory its prompts live in."""

    definition: ProfileDefinition
    directory: Path
    prompts: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.definition.profile

    @property
    def version(self) -> str:
        return self.definition.version

    def prompt_for(self, judge: JudgeSpec) -> str:
        """Return the prompt body for ``judge``."""
        key = judge.prompt or f"{judge.name}.md"
        try:
            return self.prompts[key]
        except KeyError:
            raise ConfigError(
                f"profile '{self.name}' declares judge '{judge.name}' but prompt "
                f"'{key}' is missing from {self.directory / 'prompts'}"
            ) from None

    def prompt_versions(self) -> dict[str, str]:
        """Content digests of every prompt, recorded for reproducibility."""
        import hashlib

        return {
            name: hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
            for name, body in sorted(self.prompts.items())
        }


def search_paths(root: Path, extra: list[str] | None = None) -> list[Path]:
    """Return every directory that may contain profiles, most specific first."""
    paths: list[Path] = []
    for entry in extra or []:
        candidate = Path(entry)
        paths.append(candidate if candidate.is_absolute() else root / candidate)
    paths.append(root / "profiles")
    for entry in os.environ.get(ENV_PATH_VAR, "").split(os.pathsep):
        if entry.strip():
            paths.append(Path(entry.strip()))
    for discovered in (_bundled_profiles_dir(), _source_profiles_dir()):
        if discovered is not None and discovered not in paths:
            paths.append(discovered)
    return paths


def available_profiles(root: Path, extra: list[str] | None = None) -> dict[str, Path]:
    """Map profile name to directory across every search path."""
    found: dict[str, Path] = {}
    for base in search_paths(root, extra):
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if (child / "profile.yaml").is_file() and child.name not in found:
                found[child.name] = child
    for name, directory in _entry_point_profiles().items():
        found.setdefault(name, directory)
    return found


def load_profile(name: str, root: Path, extra: list[str] | None = None) -> Profile:
    """Load the profile called ``name``."""
    catalog = available_profiles(root, extra)
    directory = catalog.get(name)
    if directory is None:
        known = ", ".join(sorted(catalog)) or "(none found)"
        raise ConfigError(f"unknown profile '{name}'. Available profiles: {known}")
    return load_profile_dir(directory)


def load_profile_dir(directory: Path) -> Profile:
    """Load a profile from an explicit directory."""
    definition_path = directory / "profile.yaml"
    if not definition_path.is_file():
        raise ConfigError(f"{directory} is not a profile: profile.yaml is missing")
    data = load_yaml(definition_path)
    rubric_path = directory / "rubric.yaml"
    if rubric_path.is_file():
        data.setdefault("rubric", load_yaml(rubric_path))
    try:
        definition = ProfileDefinition.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"invalid profile at {definition_path}: {exc}") from exc

    prompts: dict[str, str] = {}
    prompts_dir = directory / "prompts"
    if prompts_dir.is_dir():
        for prompt_file in sorted(prompts_dir.glob("*.md")):
            prompts[prompt_file.name] = prompt_file.read_text(encoding="utf-8")
    return Profile(definition=definition, directory=directory, prompts=prompts)


def _bundled_profiles_dir() -> Path | None:
    """The profiles shipped with the installed package, if they are present."""
    try:
        resource = resources.files("veritas") / "_bundled_profiles"
    except (ModuleNotFoundError, AttributeError):  # pragma: no cover - defensive
        return None
    path = Path(str(resource))
    return path if path.is_dir() else None


def _source_profiles_dir() -> Path | None:
    """The repository's own ``profiles/`` when running from a source checkout.

    An editable install (``pip install -e .``) does not materialise the bundled
    copy, so without this an artifact whose config lives in a subdirectory —
    ``examples/paper``, say — finds no profiles at all.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "profiles"
        if (candidate / "generic-document" / "profile.yaml").is_file():
            return candidate
    return None


def _entry_point_profiles() -> dict[str, Path]:
    """Discover profiles published by installed plugin packages."""
    discovered: dict[str, Path] = {}
    try:
        entry_points = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:  # pragma: no cover - importlib backend differences
        return discovered
    for entry in entry_points:
        try:
            target = entry.load()
        except Exception:  # pragma: no cover - a broken plugin must not break runs
            continue
        directory = Path(str(target() if callable(target) else target))
        if (directory / "profile.yaml").is_file():
            discovered[entry.name] = directory
        elif directory.is_dir():
            for child in sorted(directory.iterdir()):
                if (child / "profile.yaml").is_file():
                    discovered.setdefault(child.name, child)
    return discovered
