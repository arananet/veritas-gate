"""The figures judge sends rendered figure pages to a vision-capable model."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from veritas.artifacts import Artifact
from veritas.judges import EvaluationContext
from veritas.judges.visual import VisualJudge, figure_pages
from veritas.providers import build_provider
from veritas.providers.anthropic import _content as anthropic_content
from veritas.providers.base import ModelSpec
from veritas.providers.google import _parts as google_parts

CONTEXT = EvaluationContext(profile="p", profile_version="1", run_id="r")
HAS_TOOLS = all(shutil.which(tool) for tool in ("pdflatex", "pdftotext", "pdftoppm"))


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")


def paper_pdf(tmp_path: Path) -> Path:
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}\n"
        "Intro page.\\newpage\n"
        "\\begin{figure}[h]\\centering\\rule{4cm}{2cm}"
        "\\caption{Main result.}\\end{figure}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "main.tex"],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    return tmp_path / "main.pdf"


@pytest.mark.skipif(not HAS_TOOLS, reason="needs pdflatex and poppler")
def test_figure_pages_are_found(tmp_path: Path) -> None:
    assert [n for n, _ in figure_pages(paper_pdf(tmp_path))] == [2]


@pytest.mark.skipif(not HAS_TOOLS, reason="needs pdflatex and poppler")
async def test_the_judge_sends_page_images_and_parses_findings(tmp_path: Path, serve) -> None:
    pdf = paper_pdf(tmp_path)
    seen: list[dict[str, Any]] = []
    payload = {
        "status": "fail",
        "findings": [
            {
                "title": "Bars have no error bars or n",
                "severity": "major",
                "category": "figures",
                "description": "Figure 1 compares values without uncertainty.",
                "location": "Figure 1 (page 2)",
                "evidence": ["no error bars drawn"],
            }
        ],
    }

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seen.append(body)
        return 200, {"choices": [{"message": {"content": json.dumps(payload)}}]}

    with serve(handler) as server:
        provider = build_provider(ModelSpec(provider="openai", model="m", base_url=server.base_url))
        judge = VisualJudge(name="figures", prompt="# Figures", provider=provider, pdf=pdf)
        artifact = Artifact(id="a", type="paper", root=tmp_path, paths=["main.tex"])
        result = await judge.evaluate(
            artifact, EvaluationContext(profile="p", profile_version="1", run_id="r")
        )

    content = seen[0]["messages"][1]["content"]
    images = [part for part in content if part["type"] == "image_url"]
    assert len(images) == 1
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "Page 2" in content[0]["text"]
    assert result.findings[0].severity == "major"
    assert result.metadata["pages"] == [2]


async def test_a_missing_pdf_is_a_judge_error_not_a_verdict(tmp_path: Path) -> None:
    provider = build_provider(
        ModelSpec(provider="openai", model="m", base_url="http://127.0.0.1:9")
    )
    judge = VisualJudge(name="figures", prompt="# F", provider=provider, pdf=tmp_path / "none.pdf")
    artifact = Artifact(id="a", type="paper", root=tmp_path, paths=[])
    result = await judge.evaluate(
        artifact, EvaluationContext(profile="p", profile_version="1", run_id="r")
    )
    assert result.status == "error"


def test_provider_image_formats() -> None:
    png = b"\x89PNG"
    blocks = anthropic_content("look", [png])
    assert blocks[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(blocks[0]["source"]["data"]) == png
    assert blocks[-1] == {"type": "text", "text": "look"}
    assert anthropic_content("plain", None) == "plain"
    parts = google_parts("look", [png])
    assert parts[0]["inlineData"]["mimeType"] == "image/png"
    assert parts[-1] == {"text": "look"}


def test_the_visual_judge_is_skipped_without_a_pdf(tmp_path: Path, repo_root: Path) -> None:
    from veritas.config import load_config
    from veritas.engine import Engine
    from veritas.profiles import load_profile

    (tmp_path / "veritas.yaml").write_text(
        f"profile_paths: [{repo_root / 'profiles'}]\n"
        "models:\n  default:\n    provider: openai\n    model: m\n    base_url: http://127.0.0.1:9\n"
    )
    config = load_config(tmp_path / "veritas.yaml")
    engine = Engine(config, load_profile("scientific-paper", repo_root))
    assert "figures" not in [judge.name for judge in engine.build_judges()]
    config.artifact.pdf = "paper.pdf"
    assert "figures" in [judge.name for judge in engine.build_judges()]
