"""Configuration loading and profile discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from veritas.config import ConfigError, find_config, interpolate_env, load_config
from veritas.profiles import available_profiles, load_profile


def test_env_interpolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERITAS_TEST_MODEL", "some-model")
    assert interpolate_env("${VERITAS_TEST_MODEL}") == "some-model"
    assert interpolate_env("${VERITAS_MISSING:-fallback}") == "fallback"
    assert interpolate_env({"a": ["${VERITAS_TEST_MODEL}"]}) == {"a": ["some-model"]}


def test_unset_variable_without_default_is_an_error(tmp_path: Path) -> None:
    config = tmp_path / "veritas.yaml"
    config.write_text(
        "profile: generic-document\n"
        "models:\n  default:\n    provider: openai\n    model: ${VERITAS_ABSENT_MODEL}\n"
    )
    with pytest.raises(ConfigError, match="VERITAS_ABSENT_MODEL"):
        load_config(config)


def test_config_is_found_by_walking_upward(tmp_path: Path) -> None:
    (tmp_path / "veritas.yaml").write_text("profile: generic-document\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config(nested) == tmp_path / "veritas.yaml"


def test_missing_config_falls_back_to_defaults(tmp_path: Path) -> None:
    config = load_config(None, root=tmp_path)
    assert config.profile == "generic-document"
    assert config.execution.allow == []


def test_gate_policy_rejects_unknown_keys(tmp_path: Path) -> None:
    config = tmp_path / "veritas.yaml"
    config.write_text("profile: generic-document\ngate:\n  fail_on_everything: true\n")
    with pytest.raises(ConfigError):
        load_config(config)


def test_secrets_are_never_read_from_config(tmp_path: Path, repo_root: Path) -> None:
    """Config carries the NAME of an env var, never a key value."""
    config = load_config(repo_root / "examples" / "paper" / "veritas.yaml")
    dumped = config.model_dump()
    assert "api_key" not in str(dumped).lower().replace("api_key_env", "")


def test_bundled_profiles_are_discoverable(repo_root: Path) -> None:
    catalog = available_profiles(repo_root)
    assert "scientific-paper" in catalog
    assert "generic-document" in catalog


def test_scientific_paper_profile_defines_its_judges(repo_root: Path) -> None:
    profile = load_profile("scientific-paper", repo_root)
    names = [spec.name for spec in profile.definition.judges]
    assert names == [
        "methodology",
        "evidence",
        "statistics",
        "reproducibility",
        "repo-consistency",
        "citations",
        "archival",
        "presentation",
        "figures",
        "adversarial",
    ]
    for spec in profile.definition.judges:
        assert profile.prompt_for(spec).strip()


def test_prompt_versions_are_content_digests(repo_root: Path) -> None:
    profile = load_profile("scientific-paper", repo_root)
    versions = profile.prompt_versions()
    assert len(versions) == len(profile.prompts)
    assert all(len(digest) == 16 for digest in versions.values())


def test_unknown_profile_names_the_alternatives(repo_root: Path) -> None:
    with pytest.raises(ConfigError, match="Available profiles"):
        load_profile("no-such-profile", repo_root)


def test_profile_search_path_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile is a plugin: a directory anywhere on the search path."""
    plugin = tmp_path / "plugins" / "my-profile"
    (plugin / "prompts").mkdir(parents=True)
    (plugin / "profile.yaml").write_text(
        "profile: my-profile\nversion: '1'\njudges:\n  - name: only\n    prompt: only.md\n"
    )
    (plugin / "prompts" / "only.md").write_text("# Only judge\n")
    monkeypatch.setenv("VERITAS_PROFILE_PATH", str(tmp_path / "plugins"))
    profile = load_profile("my-profile", tmp_path)
    assert profile.definition.judges[0].name == "only"


def test_missing_prompt_file_is_reported_clearly(tmp_path: Path) -> None:
    plugin = tmp_path / "profiles" / "broken"
    plugin.mkdir(parents=True)
    (plugin / "profile.yaml").write_text(
        "profile: broken\njudges:\n  - name: ghost\n    prompt: ghost.md\n"
    )
    profile = load_profile("broken", tmp_path)
    with pytest.raises(ConfigError, match=r"prompt 'ghost\.md' is missing"):
        profile.prompt_for(profile.definition.judges[0])


def test_profiles_resolve_from_a_subdirectory_config(repo_root: Path) -> None:
    """Regression: `veritas evaluate examples/paper` found no profiles at all.

    The artifact's config lives in a subdirectory, so the search root is that
    subdirectory, not the repository. The packaged copy does not exist under an
    editable install, which left nothing to fall back to.
    """
    catalog = available_profiles(repo_root / "examples" / "paper")
    assert "scientific-paper" in catalog
    assert "generic-document" in catalog


def test_the_example_projects_resolve_their_own_profile(repo_root: Path) -> None:
    """Every shipped example must work straight from a clone, unmodified."""
    for example, expected in (
        ("paper", "scientific-paper"),
        ("generic-document", "generic-document"),
    ):
        root = repo_root / "examples" / example
        config = load_config(root / "veritas.yaml")
        profile = load_profile(config.profile, config.root, config.profile_paths)
        assert profile.name == expected
        assert profile.definition.judges


def test_paths_outside_the_artifact_are_relative_not_absolute(tmp_path: Path) -> None:
    """Regression: they arrived as absolute paths, home directory included.

    An absolute location travels into judge prompts, findings and repair plans
    — and a path outside the workspace is one a repair agent could write to.
    """
    from veritas.artifacts import Artifact

    (tmp_path / "paper").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "paper" / "manuscript.md").write_text("# Paper\n")
    (tmp_path / "src" / "adapter.ts").write_text("export const x = 1;\n")

    artifact = Artifact(
        id="paper",
        type="document",
        root=tmp_path / "paper",
        paths=["manuscript.md", "../src"],
    )
    assert sorted(artifact.file_list()) == ["../src/adapter.ts", "manuscript.md"]
    assert str(tmp_path) not in " ".join(artifact.file_list())
    assert artifact.external_paths() == ["../src"]


def test_an_artifact_wholly_inside_its_root_reports_no_external_paths(tmp_path: Path) -> None:
    from veritas.artifacts import Artifact

    (tmp_path / "doc.md").write_text("x\n")
    artifact = Artifact(id="a", type="document", root=tmp_path, paths=["doc.md"])
    assert artifact.external_paths() == []


def test_the_scientific_paper_profile_declares_the_archival_judge(repo_root: Path) -> None:
    profile = load_profile("scientific-paper", repo_root)
    spec = next(item for item in profile.definition.judges if item.name == "archival")
    prompt = profile.prompt_for(spec)

    for topic in ("DOI", "licence", "CITATION.cff", "preregistration", "mutable"):
        assert topic.lower() in prompt.lower(), f"the archival prompt should cover {topic}"


def test_the_shipped_citability_patterns_tell_pinned_from_mutable(repo_root: Path) -> None:
    """A link to a branch can change after review; a commit or tag cannot."""
    import re

    check = load_profile("scientific-paper", repo_root).definition.checks["citability"]
    forbidden = check.forbidden_patterns[0].pattern

    assert re.search(forbidden, "https://github.com/a/b/blob/main/src.ts", re.IGNORECASE)
    assert re.search(forbidden, "https://gitlab.com/x/y/tree/master/lib", re.IGNORECASE)
    assert not re.search(forbidden, "https://github.com/a/b/blob/a1b2c3d4/src.ts", re.IGNORECASE)
    assert not re.search(forbidden, "https://github.com/a/b/tree/v1.2.0/src", re.IGNORECASE)

    doi = check.required_patterns[0].pattern
    assert re.search(doi, "https://doi.org/10.5281/zenodo.22884173")
    assert not re.search(doi, "See the repository for details.")


def test_the_citability_check_ships_disabled(repo_root: Path) -> None:
    """These are conventions, not universal requirements: opting in is the user's call."""
    check = load_profile("scientific-paper", repo_root).definition.checks["citability"]
    assert check.enabled is False
