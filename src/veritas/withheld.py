"""Files that exist but are deliberately not supplied to judges.

A judge that cannot see a file cannot tell "archived, too large to send" from
"never existed". Naming the withheld files, and saying their absence is not a
defect, lets it report a claim resting on them as unverified instead.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath

from veritas.artifacts.base import SKIP_DIRECTORIES

MAX_LISTED = 40


def resolve_withheld(root: Path, patterns: list[str], supplied: set[str]) -> list[str]:
    """Files under ``root`` matching ``patterns``, minus anything supplied.

    Patterns that match nothing are ignored: listing a path that does not
    exist would reintroduce exactly the confusion this exists to remove.
    """
    found: set[str] = set()
    for pattern in patterns:
        matches = [root / pattern] if not any(c in pattern for c in "*?[") else root.glob(pattern)
        for match in matches:
            candidates = match.rglob("*") if match.is_dir() else [match]
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                rel = candidate.relative_to(root).as_posix()
                if any(part in SKIP_DIRECTORIES for part in PurePosixPath(rel).parts):
                    continue
                if rel not in supplied:
                    found.add(rel)
    return sorted(found)


def withheld_section(paths: list[str], reason: str | None) -> str:
    """The judge-facing rule, with a bounded listing."""
    listed = paths[:MAX_LISTED]
    lines = [f"  - {path}" for path in listed]
    rest = paths[MAX_LISTED:]
    if rest:
        by_dir = Counter(str(PurePosixPath(path).parent) for path in rest)
        lines.extend(f"  - ... and {n} more under {d}/" for d, n in sorted(by_dir.items()))
    why = f" Reason given by the operator: {reason}" if reason else ""
    return (
        f"WITHHELD FILES ({len(paths)}): these exist in the repository but were "
        f"deliberately not supplied to you.{why}\n"
        + "\n".join(lines)
        + "\nTheir absence from the supplied content is not a defect. A claim that rests "
        "only on them is unverified, not unsupported: report it at most as minor, with "
        "disposition declared. Contradictions between supplied files, and claims that no "
        "supplied or withheld file could support, are still reported normally."
    )
