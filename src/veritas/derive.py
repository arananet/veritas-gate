"""Derived files: figures, tables and generated sources with provenance.

A paper's PDF carried an older DOI than its Markdown for days, and an arXiv
bibliography lagged its .bib, because nothing tied a generated file to what it
was generated from. A derivation names the command, its inputs and its outputs;
building it records the hash of every input and output in ``veritas.lock.json``.
From then on a stale output (an input changed), a hand-edited output (the
output changed on its own) and a figure nobody can regenerate are all
detectable without running anything.

Building runs only allow-listed executables, with no shell, and refuses to
write a frozen path: a derivation may read the evidence, never replace it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from veritas.config import ConfigError, Derivation, ExecutionConfig

LOCK_NAME = "veritas.lock.json"
ALWAYS_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")


def expand(root: Path, patterns: list[str]) -> list[str]:
    """Files matching ``patterns`` under ``root``, relative and sorted."""
    found: set[str] = set()
    for pattern in patterns:
        matches = [root / pattern] if not any(c in pattern for c in "*?[") else root.glob(pattern)
        for match in matches:
            candidates = match.rglob("*") if match.is_dir() else [match]
            for path in candidates:
                if path.is_file():
                    found.add(path.relative_to(root).as_posix())
    return sorted(found)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def hashes(root: Path, paths: list[str]) -> dict[str, str]:
    return {path: digest(root / path) for path in paths if (root / path).is_file()}


def load_lock(root: Path) -> dict[str, dict[str, object]]:
    path = root / LOCK_NAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = data.get("derivations", {}) if isinstance(data, dict) else {}
    return entries if isinstance(entries, dict) else {}


def write_lock(root: Path, entries: dict[str, dict[str, object]]) -> Path:
    path = root / LOCK_NAME
    body = {
        "note": "Written by `veritas build`. Hashes of each derivation's inputs and outputs.",
        "derivations": dict(sorted(entries.items())),
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


def is_frozen(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        clean = pattern.strip().removeprefix("./").rstrip("/")
        if not clean:
            continue
        if fnmatch.fnmatchcase(path, clean) or path.startswith(clean + "/"):
            return True
        if clean.endswith("/**") and path.startswith(clean[:-3] + "/"):
            return True
    return False


@dataclass(slots=True)
class BuildResult:
    id: str
    ok: bool
    detail: str = ""
    outputs: dict[str, str] = field(default_factory=dict)


def validate(derivation: Derivation, execution: ExecutionConfig, frozen: list[str]) -> None:
    executable = derivation.command[0]
    allowed = set(execution.allow)
    if executable not in allowed and os.path.basename(executable) not in allowed:
        raise ConfigError(
            f"derivation '{derivation.id}': '{executable}' is not in execution.allow; "
            "add it to veritas.yaml to permit building"
        )
    for output in derivation.outputs:
        if is_frozen(output, frozen):
            raise ConfigError(
                f"derivation '{derivation.id}' writes '{output}', which is frozen evidence. "
                "A derivation may read the evidence, never replace it."
            )


def build(
    root: Path,
    derivations: list[Derivation],
    execution: ExecutionConfig,
    frozen: list[str],
    only: list[str] | None = None,
) -> list[BuildResult]:
    """Run each derivation and record its input and output hashes in the lock."""
    selected = [d for d in derivations if not only or d.id in only]
    for derivation in selected:
        validate(derivation, execution, frozen)
    lock = load_lock(root)
    env = {
        name: os.environ[name]
        for name in (*ALWAYS_PASSTHROUGH, *execution.env_passthrough)
        if name in os.environ
    }
    results: list[BuildResult] = []
    backups: list[Path] = []
    try:
        results = _run_all(root, selected, env, frozen, lock, backups)
    finally:
        for backup in backups:
            shutil.rmtree(backup, ignore_errors=True)
    write_lock(root, lock)
    return results


def _run_all(
    root: Path,
    selected: list[Derivation],
    env: dict[str, str],
    frozen: list[str],
    lock: dict[str, dict[str, object]],
    backups: list[Path],
) -> list[BuildResult]:
    results: list[BuildResult] = []
    for derivation in selected:
        cwd = (root / derivation.working_dir).resolve() if derivation.working_dir else root
        frozen_files = expand(root, frozen)
        frozen_before = hashes(root, frozen_files)
        backup = _backup(root, frozen_files)
        if backup is not None:
            backups.append(backup)
        try:
            completed = subprocess.run(
                derivation.command,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=derivation.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append(BuildResult(derivation.id, False, str(exc)))
            continue
        if hashes(root, list(frozen_before)) != frozen_before or set(expand(root, frozen)) != set(
            frozen_files
        ):
            restored = _restore(root, backup, frozen_files)
            results.append(
                BuildResult(
                    derivation.id,
                    False,
                    f"changed frozen evidence; build rejected and {restored} file(s) restored",
                )
            )
            continue
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout)[-800:]
            results.append(
                BuildResult(derivation.id, False, f"exit {completed.returncode}: {tail}")
            )
            continue
        outputs = expand(root, derivation.outputs)
        missing = [
            o
            for o in derivation.outputs
            if not any(
                x == o or x.startswith(o.rstrip("/") + "/") or fnmatch.fnmatchcase(x, o)
                for x in outputs
            )
        ]
        if missing:
            results.append(
                BuildResult(derivation.id, False, f"did not produce {', '.join(missing)}")
            )
            continue
        output_hashes = hashes(root, outputs)
        lock[derivation.id] = {
            "command": derivation.command,
            "kind": derivation.kind,
            "inputs": hashes(root, expand(root, derivation.inputs)),
            "outputs": output_hashes,
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        results.append(BuildResult(derivation.id, True, outputs=output_hashes))
    return results


def _produced(pattern: str, outputs: list[str]) -> bool:
    prefix = pattern.rstrip("/") + "/"
    return any(
        x == pattern or x.startswith(prefix) or fnmatch.fnmatchcase(x, pattern) for x in outputs
    )


def _backup(root: Path, files: list[str]) -> Path | None:
    """Copy frozen files aside, so a script that rewrites one cannot keep the change."""
    if not files:
        return None
    backup = Path(tempfile.mkdtemp(prefix="veritas-frozen-"))
    for rel in files:
        target = backup / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, target)
    return backup


def _restore(root: Path, backup: Path | None, files: list[str]) -> int:
    if backup is None:
        return 0
    restored = 0
    for rel in files:
        original = backup / rel
        current = root / rel
        if not current.is_file() or digest(current) != digest(original):
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, current)
            restored += 1
    return restored
