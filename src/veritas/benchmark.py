"""Measure Veritas against papers whose problems are already known.

"Veritas is a good gate" is a claim, and like any claim it needs evidence. A
benchmark is a list of papers, each with the problems a human already found in
it -- a reviewer on OpenReview, a published erratum, a retraction notice -- or
planted on purpose. Veritas evaluates each paper blind, and the benchmark
reports two numbers:

* recall: of the known problems, how many a finding matched;
* unmatched findings: what Veritas reported that no known problem explains.

The second is not a false-positive rate. A paper's known problems are rarely
all of its problems, so an unmatched finding is a candidate false positive for
a human to read, not a proven one. It is reported that way.

Matching is deterministic -- regular expressions over a finding's text -- so a
saved benchmark can be rescored after the patterns are refined, without
spending another token.
"""

from __future__ import annotations

import gzip
import io
import re
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from veritas.config import ConfigError, VeritasConfig
from veritas.models.evaluation import EvaluationResult
from veritas.models.finding import Finding, Severity

SEVERITY_RANK: dict[str, int] = {"info": 0, "minor": 1, "major": 2, "critical": 3}
_ARXIV_ID = re.compile(r"arxiv\.org/(?:abs|pdf|e-print)/([^\s/?#]+?)(?:\.pdf)?$|^arxiv:(\S+)$")


class KnownIssue(BaseModel):
    """A problem a human already found, and how to recognise a finding about it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    # Every pattern must match somewhere in a finding's title, description,
    # location, evidence or recommendation (case-insensitive). Use `a|b` inside
    # one pattern for alternatives.
    match: list[str] = Field(min_length=1)
    # A finding below this severity does not count as detecting the issue: a
    # fatal flaw reported as a minor nit was not caught.
    min_severity: Severity = "minor"
    source: str | None = None  # where the issue is documented

    def matches(self, finding: Finding) -> bool:
        if SEVERITY_RANK[finding.severity] < SEVERITY_RANK[self.min_severity]:
            return False
        text = "\n".join(
            [
                finding.title,
                finding.description,
                finding.location or "",
                finding.recommendation or "",
                *finding.evidence,
            ]
        )
        return all(re.search(pattern, text, re.IGNORECASE) for pattern in self.match)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    git: str | None = None
    ref: str | None = None
    arxiv: str | None = None

    @model_validator(mode="after")
    def _one(self) -> Source:
        if sum(value is not None for value in (self.path, self.git, self.arxiv)) != 1:
            raise ValueError("a source needs exactly one of path, git or arxiv")
        return self

    @classmethod
    def from_url(cls, value: str) -> Source:
        """Accept a bare URL: an arXiv page, a git repository, or a local path."""
        match = _ARXIV_ID.search(value.strip())
        if match:
            return cls(arxiv=match.group(1) or match.group(2))
        if value.startswith(("https://", "http://", "git@")):
            return cls(git=value)
        return cls(path=value)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    source: Source
    profile: str | None = None
    paths: list[str] = Field(default_factory=lambda: ["."])
    thesis: list[str] = Field(default_factory=list)
    known_issues: list[KnownIssue] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _url(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("source"), str):
            data = {**data, "source": Source.from_url(data["source"])}
        return data


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    cases: list[Case] = Field(min_length=1)


def load_manifest(path: Path) -> Manifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return Manifest.model_validate(raw)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigError(f"benchmark manifest {path}: {exc}") from exc


# ---------------------------------------------------------------- scoring


class IssueScore(BaseModel):
    id: str
    description: str
    detected: bool
    matched_by: list[str] = Field(default_factory=list)


class CaseScore(BaseModel):
    case: str
    status: Literal["scored", "error"] = "scored"
    error: str | None = None
    gate: str | None = None
    issues: list[IssueScore] = Field(default_factory=list)
    findings: int = 0
    unmatched: list[str] = Field(default_factory=list)
    unmatched_blocking: int = 0
    tokens: int | None = None
    cost: float | None = None

    @property
    def detected(self) -> int:
        return sum(issue.detected for issue in self.issues)


class BenchmarkScore(BaseModel):
    created_at: datetime
    cases: list[CaseScore]

    @property
    def known(self) -> int:
        return sum(len(case.issues) for case in self.cases if case.status == "scored")

    @property
    def detected(self) -> int:
        return sum(case.detected for case in self.cases if case.status == "scored")

    @property
    def recall(self) -> float | None:
        return self.detected / self.known if self.known else None

    def to_json(self) -> dict[str, object]:
        body = self.model_dump(mode="json")
        body["summary"] = {
            "known_issues": self.known,
            "detected": self.detected,
            "recall": self.recall,
            "unmatched_findings": sum(len(case.unmatched) for case in self.cases),
            "unmatched_blocking": sum(case.unmatched_blocking for case in self.cases),
            "errors": sum(case.status == "error" for case in self.cases),
        }
        return body


def all_findings(result: EvaluationResult) -> list[Finding]:
    """Judge findings after consolidation, plus deterministic check findings."""
    seen: set[str] = set()
    findings: list[Finding] = []
    for finding in [
        *result.meta_review.findings,
        *(f for check in result.check_results for f in check.findings),
    ]:
        if finding.id not in seen:
            seen.add(finding.id)
            findings.append(finding)
    return findings


def score_case(case: Case, result: EvaluationResult) -> CaseScore:
    findings = all_findings(result)
    explained: set[str] = set()
    issues: list[IssueScore] = []
    for issue in case.known_issues:
        hits = [finding.id for finding in findings if issue.matches(finding)]
        explained.update(hits)
        issues.append(
            IssueScore(
                id=issue.id, description=issue.description, detected=bool(hits), matched_by=hits
            )
        )
    blocking = set(result.gate.blocking_findings)
    unmatched = [f for f in findings if f.id not in explained]
    usage = result.manifest.usage or {}
    return CaseScore(
        case=case.id,
        gate=result.gate.status,
        issues=issues,
        findings=len(findings),
        unmatched=[f"{f.id} [{f.severity}] {f.title}" for f in unmatched],
        unmatched_blocking=sum(f.id in blocking for f in unmatched),
        tokens=_tokens(usage),
        cost=_float(usage.get("cost")),
    )


def _tokens(usage: dict[str, object]) -> int | None:
    parts = [usage.get("input_tokens"), usage.get("output_tokens")]
    if not any(isinstance(part, int) for part in parts):
        return None
    return sum(part for part in parts if isinstance(part, int))


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def render_markdown(score: BenchmarkScore) -> str:
    recall = f"{score.recall:.0%}" if score.recall is not None else "n/a"
    lines = [
        "# Veritas benchmark",
        "",
        f"Known issues detected: **{score.detected}/{score.known}** (recall {recall}).",
        "",
        "Unmatched findings are candidates for a human to read, not proven false "
        "positives: a paper's known issues are rarely all of its issues.",
        "",
        "| Case | Gate | Detected | Findings | Unmatched (blocking) | Tokens |",
        "|---|---|---|---|---|---|",
    ]
    for case in score.cases:
        if case.status == "error":
            lines.append(f"| {case.case} | error | - | - | - | - |")
            continue
        lines.append(
            f"| {case.case} | {case.gate} | {case.detected}/{len(case.issues)} | "
            f"{case.findings} | {len(case.unmatched)} ({case.unmatched_blocking}) | "
            f"{case.tokens if case.tokens is not None else '-'} |"
        )
    for case in score.cases:
        lines += ["", f"## {case.case}", ""]
        if case.error:
            lines.append(f"Error: {case.error}")
            continue
        for issue in case.issues:
            mark = "✓" if issue.detected else "✗"
            by = f" — {', '.join(issue.matched_by)}" if issue.matched_by else ""
            lines.append(f"- {mark} **{issue.id}**: {issue.description}{by}")
        if case.unmatched:
            lines += ["", "Unmatched:"] + [f"- {item}" for item in case.unmatched]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- fetching


def fetch(case: Case, manifest_dir: Path, cache: Path) -> Path:
    """Materialise a case's source. Remote sources are cached by case id.

    Fetching downloads files; it never runs anything from them.
    """
    source = case.source
    if source.path is not None:
        target = (manifest_dir / source.path).resolve()
        if not target.exists():
            raise ConfigError(f"case {case.id}: {target} does not exist")
        return target
    destination = cache / case.id
    if destination.exists():
        return destination
    cache.mkdir(parents=True, exist_ok=True)
    staging = cache / f".{case.id}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if source.git is not None:
            _git(source.git, source.ref, staging)
        else:
            assert source.arxiv is not None
            _arxiv(source.arxiv, staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    staging.rename(destination)
    return destination


def _git(url: str, ref: str | None, destination: Path) -> None:
    def run(*args: str) -> None:
        completed = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=600, check=False
        )
        if completed.returncode != 0:
            raise ConfigError(f"git {args[0]} failed for {url}: {completed.stderr.strip()}")

    if ref is None:
        run("clone", "--depth", "1", url, str(destination))
        return
    destination.mkdir(parents=True)
    run("-C", str(destination), "init", "-q")
    run("-C", str(destination), "fetch", "-q", "--depth", "1", url, ref)
    run("-C", str(destination), "checkout", "-q", "FETCH_HEAD")


def _arxiv(identifier: str, destination: Path) -> None:
    """Download an arXiv submission's source (a tarball, or one gzipped .tex)."""
    url = f"https://arxiv.org/e-print/{identifier}"
    response = httpx.get(url, follow_redirects=True, timeout=120.0)
    if response.status_code != 200:
        raise ConfigError(f"arXiv {identifier}: HTTP {response.status_code} from {url}")
    unpack_arxiv(response.content, destination)


def unpack_arxiv(payload: bytes, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    data = gzip.decompress(payload) if payload[:2] == b"\x1f\x8b" else payload
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            # The "data" filter refuses absolute paths, links outside the tree
            # and device files: an archive is content, not instructions.
            archive.extractall(destination, filter="data")
    except tarfile.ReadError:
        if data[:4] == b"%PDF":
            raise ConfigError("arXiv has only a PDF for this paper, no source") from None
        if data.lstrip()[:1] == b"%" or b"\\documentclass" in data[:4096]:
            (destination / "main.tex").write_bytes(data)
        else:
            raise ConfigError("arXiv returned an unrecognised source format") from None


def benchmark_dir(root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = root / ".veritas" / "benchmark" / stamp
    path.mkdir(parents=True, exist_ok=False)
    return path


# ---------------------------------------------------------------- running


async def evaluate_case(
    case: Case, source: Path, base: VeritasConfig, *, profile_name: str | None = None
) -> EvaluationResult:
    """Evaluate one case with the operator's models and the case's own scope.

    Only models, pricing and limits come from the operator's configuration;
    what is evaluated, and against which claims, comes from the case.
    """
    from veritas.artifacts.base import Artifact
    from veritas.engine import Engine
    from veritas.profiles import load_profile

    config = base.model_copy(deep=True)
    config.root = base.root
    config.artifact.paths = list(case.paths)
    config.artifact.withheld = []
    config.thesis = list(case.thesis)
    # The operator's paper-specific settings do not transfer to someone
    # else's paper: its scopes, check targets, gate and accepted risks.
    config.judge_paths = {}
    config.checks = {}
    config.gate = None
    config.metadata = {}
    name = case.profile or profile_name or config.profile
    profile = load_profile(name, base.root, config.profile_paths)
    artifact = Artifact(
        id=case.id,
        type=config.artifact.type or profile.definition.artifact_type,
        root=source,
        paths=list(case.paths),
        max_file_chars=config.artifact.max_file_chars,
    )
    return await Engine(config, profile).run(artifact)
