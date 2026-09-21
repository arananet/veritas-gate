"""Loop persistence.

Every iteration is written before the next begins, and nothing historical is
ever overwritten. The directory *is* the audit trail:

```text
.veritas/loops/<loop-id>/
├── loop.json
├── loop-result.json
├── loop-report.md
├── ledger.json
├── final.patch
├── iteration-001/
│   ├── evaluation/          (a complete evaluation run)
│   ├── findings-before.json
│   ├── repair-plan.json
│   ├── repair-result.json
│   └── changes.patch
└── iteration-002/
    └── ... plus delta.json
```
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from veritas.models.evaluation import EvaluationResult
from veritas.models.loop import EvaluationDelta, LoopResult
from veritas.models.repair import RepairPlan, RepairResult
from veritas.repair.workspace import Workspace
from veritas.runs import write_run

LATEST_POINTER = "latest"


def new_loop_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H%M%SZ")
    return f"loop-{stamp}-{secrets.token_hex(2)}"


class LoopStore:
    """Writes one loop's audit trail."""

    def __init__(self, base: Path, loop_id: str | None = None) -> None:
        self.base = base
        self.loop_id = loop_id or new_loop_id()
        self.root = base / self.loop_id
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- layout

    def iteration_dir(self, iteration: int) -> Path:
        path = self.root / f"iteration-{iteration:03d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ledger_path(self) -> Path:
        return self.root / "ledger.json"

    # -------------------------------------------------------------- writes

    def write_evaluation(self, iteration_dir: Path, result: EvaluationResult) -> None:
        from veritas.reports import render_markdown

        write_run(iteration_dir / "evaluation", result, render_markdown(result))

    def write_findings_before(self, iteration_dir: Path, result: EvaluationResult) -> None:
        """The findings this iteration started from, kept separate from the run."""
        _dump(
            iteration_dir / "findings-before.json",
            {
                "run_id": result.manifest.run_id,
                "gate": result.gate.model_dump(mode="json"),
                "findings": [item.model_dump(mode="json") for item in result.meta_review.findings],
            },
        )

    def write_plan(self, iteration_dir: Path, plan: RepairPlan) -> None:
        _dump(iteration_dir / "repair-plan.json", plan.model_dump(mode="json"))

    def write_repair(self, iteration_dir: Path, repair: RepairResult) -> None:
        _dump(iteration_dir / "repair-result.json", repair.model_dump(mode="json"))

    def write_delta(self, iteration_dir: Path, delta: EvaluationDelta) -> None:
        _dump(iteration_dir / "delta.json", delta.model_dump(mode="json"))

    def write_patch(self, iteration_dir: Path, workspace: Workspace) -> bool:
        """Write this iteration's patch. Returns False when git is unavailable."""
        patch = workspace.diff()
        if not patch.strip():
            return False
        (iteration_dir / "changes.patch").write_text(patch, encoding="utf-8")
        return True

    def write_result(self, result: LoopResult, report: str, workspace: Workspace) -> None:
        _dump(self.root / "loop-result.json", result.model_dump(mode="json"))
        _dump(
            self.root / "loop.json",
            {
                "loop_id": result.loop_id,
                "profile": result.profile,
                "artifact": result.artifact_id,
                "mode": result.mode,
                "stop_reason": result.stop_reason.value,
                "iterations": len(result.iterations),
                "workspace": result.workspace,
                "workspace_mode": workspace.mode,
                "original_commit": result.original_commit,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
            },
        )
        (self.root / "loop-report.md").write_text(report, encoding="utf-8")
        patch = workspace.diff()
        if patch.strip():
            (self.root / "final.patch").write_text(patch, encoding="utf-8")
        (self.base / LATEST_POINTER).write_text(self.loop_id + "\n", encoding="utf-8")


def latest_loop_dir(base: Path) -> Path | None:
    pointer = base / LATEST_POINTER
    if pointer.is_file():
        candidate = base / pointer.read_text(encoding="utf-8").strip()
        if candidate.is_dir():
            return candidate
    if not base.is_dir():
        return None
    candidates = [path for path in base.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_loop_result(loop_dir: Path) -> LoopResult:
    payload = json.loads((loop_dir / "loop-result.json").read_text(encoding="utf-8"))
    return LoopResult.model_validate(payload)


def _dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
