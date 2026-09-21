"""Document artifacts: a manuscript, a specification, a design write-up."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from veritas.artifacts.base import Artifact


def load_document(
    root: Path,
    paths: list[str],
    *,
    artifact_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Artifact:
    return Artifact(
        id=artifact_id or root.name,
        type="document",
        root=root,
        paths=paths or ["."],
        metadata=metadata or {},
    )
