"""Create the citation and licensing files a piece of work needs.

Veritas never invents what goes in them. The operator declares metadata once --
stable facts such as name, ORCID and preferred licences in a user-level file,
per-work facts such as title, repository and DOI in veritas.yaml -- and scaffold
renders the profile's files from it. A file that already exists is left alone;
a file whose required metadata is missing is not written, and the missing field
is named.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from veritas.config import ConfigError, ProjectMetadata
from veritas.profiles import Profile, ScaffoldEntry

USER_METADATA_ENV = "VERITAS_METADATA"
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


@dataclass(slots=True)
class ScaffoldOutcome:
    path: str
    action: str  # "written", "would write", "exists", "missing metadata"
    detail: str = ""


def user_metadata_path() -> Path:
    """Where stable, cross-project metadata lives."""
    override = os.environ.get(USER_METADATA_ENV)
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "veritas" / "metadata.yaml"


def resolve_metadata(project: dict[str, Any], user_file: Path | None = None) -> ProjectMetadata:
    """Merge user-level metadata with the project's, the project winning per field."""
    merged: dict[str, Any] = {}
    path = user_file if user_file is not None else user_metadata_path()
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must contain a mapping")
        merged = _merge(merged, loaded)
    merged = _merge(merged, project or {})
    try:
        return ProjectMetadata.model_validate(merged)
    except ValueError as exc:
        raise ConfigError(f"invalid metadata: {exc}") from exc


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


def scaffold(
    root: Path,
    profile: Profile,
    metadata: ProjectMetadata,
    *,
    dry_run: bool = False,
) -> list[ScaffoldOutcome]:
    """Write every missing file the profile declares. Never overwrite."""
    outcomes: list[ScaffoldOutcome] = []
    for entry in profile.definition.scaffold:
        target = root / entry.path
        if target.exists():
            outcomes.append(ScaffoldOutcome(entry.path, "exists"))
            continue
        missing = [field for field in entry.requires if not _present(metadata, field)]
        if missing:
            outcomes.append(ScaffoldOutcome(entry.path, "missing metadata", ", ".join(missing)))
            continue
        content = render(entry, profile, metadata)
        if dry_run:
            outcomes.append(ScaffoldOutcome(entry.path, "would write"))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        outcomes.append(ScaffoldOutcome(entry.path, "written"))
    return outcomes


def render(entry: ScaffoldEntry, profile: Profile, metadata: ProjectMetadata) -> str:
    if entry.generator == "citation-cff":
        return citation_cff(metadata)
    if entry.generator:
        raise ConfigError(
            f"scaffold entry '{entry.path}' names unknown generator '{entry.generator}'"
        )
    if not entry.template:
        raise ConfigError(f"scaffold entry '{entry.path}' has neither a generator nor a template")
    template = profile.directory / "scaffold" / entry.template
    if not template.is_file():
        raise ConfigError(f"scaffold template {template} does not exist")
    text = template.read_text(encoding="utf-8")

    def substitute(match: re.Match[str]) -> str:
        value = _lookup(metadata, match.group(1))
        if value is None:
            raise ConfigError(
                f"template {entry.template} uses '{match.group(1)}', which the metadata "
                "does not provide; declare it, or list it under the entry's requires"
            )
        return str(value)

    return _PLACEHOLDER.sub(substitute, text)


def citation_cff(metadata: ProjectMetadata) -> str:
    """Citation File Format 1.2.0, built as data so quoting is always valid."""
    authors: list[dict[str, Any]] = []
    for author in metadata.authors:
        entry: dict[str, Any] = {"family-names": author.family, "given-names": author.given}
        if author.orcid:
            orcid = author.orcid.strip()
            entry["orcid"] = orcid if orcid.startswith("http") else f"https://orcid.org/{orcid}"
        if author.affiliation:
            entry["affiliation"] = author.affiliation
        authors.append(entry)
    document: dict[str, Any] = {
        "cff-version": "1.2.0",
        "message": "If you use this work, please cite it using the metadata below.",
        "title": metadata.title,
        "authors": authors,
    }
    if metadata.repository:
        document["repository-code"] = metadata.repository
    if metadata.licence.content:
        document["license"] = metadata.licence.content
    # A DOI appears only once the operator has one. It is never generated.
    if metadata.doi:
        document["doi"] = metadata.doi
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=88)


def _present(metadata: ProjectMetadata, field: str) -> bool:
    value = _lookup(metadata, field)
    return value not in (None, "", [])


def _lookup(metadata: ProjectMetadata, dotted: str) -> Any:
    if dotted == "authors.names":
        return ", ".join(f"{a.given} {a.family}" for a in metadata.authors) or None
    current: Any = metadata
    for part in dotted.split("."):
        current = current.get(part) if isinstance(current, dict) else getattr(current, part, None)
        if current is None:
            return None
    return current
