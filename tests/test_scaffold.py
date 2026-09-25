"""Scaffolding citation and licensing files from declared metadata."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from veritas.config import ConfigError, ProjectMetadata
from veritas.profiles import load_profile
from veritas.scaffold import citation_cff, resolve_metadata, scaffold

FULL = {
    "title": "A Paper",
    "authors": [{"given": "Eduardo", "family": "Arana", "orcid": "0009-0000-6435-6872"}],
    "repository": "https://github.com/example/paper",
    "licence": {"code": "MIT", "content": "CC-BY-4.0"},
}


@pytest.fixture
def profile(repo_root: Path):
    return load_profile("scientific-paper", repo_root)


def test_missing_files_are_written_from_metadata(tmp_path: Path, profile) -> None:
    outcomes = scaffold(tmp_path, profile, ProjectMetadata.model_validate(FULL))

    assert {o.path: o.action for o in outcomes} == {
        "CITATION.cff": "written",
        "LICENSING.md": "written",
    }
    licensing = (tmp_path / "LICENSING.md").read_text()
    assert "MIT" in licensing and "CC-BY-4.0" in licensing
    assert "{{" not in licensing


def test_an_existing_file_is_never_overwritten(tmp_path: Path, profile) -> None:
    (tmp_path / "CITATION.cff").write_text("mine\n", encoding="utf-8")
    outcomes = scaffold(tmp_path, profile, ProjectMetadata.model_validate(FULL))
    assert {o.path: o.action for o in outcomes}["CITATION.cff"] == "exists"
    assert (tmp_path / "CITATION.cff").read_text() == "mine\n"


def test_missing_metadata_blocks_the_file_and_names_the_field(tmp_path: Path, profile) -> None:
    """Nothing is invented: no licence declared, no licence written."""
    partial = {**FULL, "licence": {"code": "MIT"}}
    outcomes = {
        o.path: o for o in scaffold(tmp_path, profile, ProjectMetadata.model_validate(partial))
    }

    assert outcomes["LICENSING.md"].action == "missing metadata"
    assert "licence.content" in outcomes["LICENSING.md"].detail
    assert not (tmp_path / "LICENSING.md").exists()


def test_dry_run_writes_nothing(tmp_path: Path, profile) -> None:
    outcomes = scaffold(tmp_path, profile, ProjectMetadata.model_validate(FULL), dry_run=True)
    assert all(o.action == "would write" for o in outcomes)
    assert list(tmp_path.iterdir()) == []


def test_project_metadata_overrides_user_metadata_per_field(tmp_path: Path) -> None:
    user = tmp_path / "metadata.yaml"
    user.write_text(
        yaml.safe_dump(
            {
                "authors": [
                    {"given": "Eduardo", "family": "Arana", "orcid": "0009-0000-6435-6872"}
                ],
                "licence": {"code": "MIT", "content": "CC-BY-4.0"},
            }
        ),
        encoding="utf-8",
    )
    resolved = resolve_metadata(
        {"title": "This paper", "licence": {"code": "Apache-2.0"}}, user_file=user
    )
    assert resolved.title == "This paper"
    assert resolved.licence.code == "Apache-2.0"  # the project wins
    assert resolved.licence.content == "CC-BY-4.0"  # inherited from the user file
    assert resolved.authors[0].orcid == "0009-0000-6435-6872"


def test_the_citation_file_is_valid_and_carries_no_invented_doi() -> None:
    parsed = yaml.safe_load(citation_cff(ProjectMetadata.model_validate(FULL)))
    assert parsed["cff-version"] == "1.2.0"
    assert parsed["authors"][0]["orcid"] == "https://orcid.org/0009-0000-6435-6872"
    assert parsed["license"] == "CC-BY-4.0"
    assert "doi" not in parsed


def test_a_declared_doi_appears() -> None:
    parsed = yaml.safe_load(
        citation_cff(ProjectMetadata.model_validate({**FULL, "doi": "10.5281/zenodo.1"}))
    )
    assert parsed["doi"] == "10.5281/zenodo.1"


def test_an_unknown_template_field_is_an_error(tmp_path: Path, profile) -> None:
    template = profile.directory / "scaffold" / "LICENSING.md"
    original = template.read_text()
    try:
        template.write_text("{{licence.nonexistent}}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=r"licence\.nonexistent"):
            scaffold(tmp_path, profile, ProjectMetadata.model_validate(FULL))
    finally:
        template.write_text(original, encoding="utf-8")


def test_the_scientific_paper_profile_checks_for_the_files(profile) -> None:
    check = profile.definition.checks["archival-files"]
    assert {"CITATION.cff", "LICENSE", "LICENSING.md"} <= set(check.required_paths)
    assert {e.path for e in profile.definition.scaffold} == {"CITATION.cff", "LICENSING.md"}
