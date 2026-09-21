"""Immutable run storage under ``.veritas/runs/<timestamp>/``.

A run records the provider, model, configuration, prompt digests, judge
versions, artifact commit and every raw result, so an evaluation can be
reproduced and audited later.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from veritas.models.evaluation import EvaluationResult

RUN_ID_FORMAT = "%Y-%m-%dT%H%M%SZ"
LATEST_POINTER = "latest"


def new_run_id(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime(RUN_ID_FORMAT)


def unique_run_dir(runs_dir: Path, run_id: str) -> Path:
    """Return a fresh directory for ``run_id``; runs are never overwritten."""
    candidate = runs_dir / run_id
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = runs_dir / f"{run_id}-{suffix}"
    candidate.mkdir(parents=True)
    return candidate


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_run(run_dir: Path, result: EvaluationResult, report_markdown: str) -> None:
    """Persist every artefact of a run."""
    _dump(run_dir / "manifest.json", result.manifest.model_dump(mode="json"))
    _dump(run_dir / "results.json", result.model_dump(mode="json"))
    _dump(run_dir / "gate.json", result.gate.model_dump(mode="json"))
    _dump(
        run_dir / "claims.json",
        {
            "claims": [claim.model_dump(mode="json") for claim in result.claims],
            "evidence": [item.model_dump(mode="json") for item in result.evidence],
            "coverage": result.coverage.model_dump(mode="json"),
        },
    )
    _dump(run_dir / "meta.json", result.meta_review.model_dump(mode="json"))
    for judge_result in result.judge_results:
        _dump(
            run_dir / "judges" / f"{judge_result.judge}.json", judge_result.model_dump(mode="json")
        )
    for check_result in result.check_results:
        _dump(
            run_dir / "checks" / f"{check_result.check}.json", check_result.model_dump(mode="json")
        )
    if result.stability:
        _dump(
            run_dir / "stability.json",
            [item.model_dump(mode="json") for item in result.stability],
        )
    _dump(run_dir / "repair-plan.json", build_repair_plan(result))
    (run_dir / "report.md").write_text(report_markdown, encoding="utf-8")
    _write_latest_pointer(run_dir)


def _write_latest_pointer(run_dir: Path) -> None:
    """Record the newest run without relying on symlink support."""
    (run_dir.parent / LATEST_POINTER).write_text(run_dir.name + "\n", encoding="utf-8")


def build_repair_plan(result: EvaluationResult) -> dict[str, Any]:
    """Describe what would fix the run. Veritas never applies it itself."""
    return {
        "run_id": result.manifest.run_id,
        "gate": result.gate.status,
        "note": (
            "Veritas Gate does not modify artifacts. This plan is advisory; "
            "repair is a separate, explicitly invoked step."
        ),
        "actions": [
            {"finding": action.finding, "action": action.action, "files": action.files}
            for action in result.meta_review.required_actions
        ],
    }


def latest_run_dir(runs_dir: Path) -> Path | None:
    """Return the most recent run directory, or None when there is none."""
    pointer = runs_dir / LATEST_POINTER
    if pointer.is_file():
        name = pointer.read_text(encoding="utf-8").strip()
        candidate = runs_dir / name
        if candidate.is_dir():
            return candidate
    if not runs_dir.is_dir():
        return None
    candidates = [path for path in runs_dir.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_run(run_dir: Path) -> EvaluationResult:
    """Read a persisted run back into memory."""
    payload = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    return EvaluationResult.model_validate(payload)
