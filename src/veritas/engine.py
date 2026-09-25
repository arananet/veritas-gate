"""Evaluation orchestration.

The engine is domain-agnostic. It knows about artifacts, judges, checks, claims,
the meta review and the gate — never about papers, repositories or architecture
reviews. Those are profiles, and adding one requires no change here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from veritas import __version__
from veritas.artifacts.base import Artifact
from veritas.checks import build_check
from veritas.claims import build_graph, claim_findings
from veritas.claims.graph import ClaimGraph
from veritas.config import (
    CheckConfig,
    ConfigError,
    GatePolicy,
    ModelConfig,
    PricingConfig,
    VeritasConfig,
)
from veritas.gate import evaluate_gate
from veritas.judges.base import EvaluationContext
from veritas.judges.llm import LLMJudge
from veritas.judges.meta import MetaJudge
from veritas.models.evaluation import (
    CheckResult,
    EvaluationResult,
    JudgeResult,
    JudgeStability,
    MetaReview,
    RunManifest,
)
from veritas.models.finding import Finding, max_severity, severity_rank
from veritas.profiles import JudgeSpec, Profile
from veritas.providers import ModelProvider, ModelSpec, build_provider
from veritas.runs import new_run_id
from veritas.triage import settle
from veritas.usage import (
    META_JUDGE_LABEL,
    CallUsage,
    UsageSummary,
    call_from_metadata,
    summarize,
)

ProgressFn = Callable[[str, str, str], None]
"""``(phase, name, status)`` — status is one of ``start``, ``ok``, ``warn``, ``fail``."""


def _noop(phase: str, name: str, status: str) -> None:  # pragma: no cover - default
    return None


@dataclass(slots=True)
class EngineOptions:
    """Per-invocation switches that do not belong in persisted configuration."""

    judge_filter: list[str] = field(default_factory=list)
    runs: int = 1
    skip_checks: bool = False
    progress: ProgressFn = _noop
    provider_factory: Callable[[ModelSpec], ModelProvider] = build_provider


class Engine:
    """Runs one evaluation end to end."""

    def __init__(
        self,
        config: VeritasConfig,
        profile: Profile,
        options: EngineOptions | None = None,
    ) -> None:
        self.config = config
        self.profile = profile
        self.options = options or EngineOptions()
        self._providers: dict[str, ModelProvider] = {}

    # ------------------------------------------------------------------ setup

    def model_config_for(self, role: str) -> ModelConfig:
        """Resolve the model for a judge role, falling back to ``default``."""
        models = self.config.models
        for key in (role, "default"):
            if key in models:
                return models[key]
        raise ConfigError(
            f"no model configured for role '{role}'. Add models.{role} (or models.default) "
            "to veritas.yaml; see veritas.yaml.example and .env.example."
        )

    def provider_for(self, role: str) -> ModelProvider:
        if role not in self._providers:
            model = self.model_config_for(role)
            spec = ModelSpec(
                provider=model.provider,
                model=model.model,
                temperature=model.temperature,
                max_tokens=model.max_tokens,
                timeout=model.timeout,
                max_retries=model.max_retries,
                base_url=model.base_url,
                api_key_env=model.api_key_env,
                tls_verify=model.tls_verify,
                extra=model.model_extra or {},
            )
            self._providers[role] = self.options.provider_factory(spec)
        return self._providers[role]

    def judge_specs(self) -> list[JudgeSpec]:
        specs = [spec for spec in self.profile.definition.judges if spec.enabled]
        if self.config.judges is not None:
            wanted = set(self.config.judges)
            specs = [spec for spec in specs if spec.name in wanted]
        if self.options.judge_filter:
            wanted = set(self.options.judge_filter)
            unknown = wanted - {spec.name for spec in self.profile.definition.judges}
            if unknown:
                known = ", ".join(spec.name for spec in self.profile.definition.judges)
                raise ConfigError(
                    f"unknown judge(s): {', '.join(sorted(unknown))}. "
                    f"Profile '{self.profile.name}' defines: {known}"
                )
            specs = [spec for spec in specs if spec.name in wanted]
        if not specs:
            raise ConfigError(
                f"profile '{self.profile.name}' has no enabled judges matching the selection"
            )
        return specs

    def build_judges(self) -> list[LLMJudge]:
        judges: list[LLMJudge] = []
        for spec in self.judge_specs():
            role = spec.model_role or spec.name
            judges.append(
                LLMJudge(
                    name=spec.name,
                    prompt=self.profile.prompt_for(spec),
                    provider=self.provider_for(role),
                    version=spec.version,
                    extracts_claims=spec.extracts_claims,
                    model_role=role,
                )
            )
        return judges

    def check_configs(self) -> dict[str, CheckConfig]:
        """Profile checks, overridden by anything the project configured."""
        merged: dict[str, CheckConfig] = dict(self.profile.definition.checks)
        merged.update(self.config.checks)
        return {name: config for name, config in merged.items() if config.enabled}

    def gate_policy(self) -> GatePolicy:
        return self.config.gate or self.profile.definition.gate

    # --------------------------------------------------------------- pipeline

    async def run(self, artifact: Artifact) -> EvaluationResult:
        started = datetime.now(UTC)
        run_id = new_run_id(started)
        context = EvaluationContext(
            profile=self.profile.name,
            profile_version=self.profile.version,
            run_id=run_id,
            rubric=self.profile.definition.rubric,
            artifact_type=artifact.type,
            thesis=list(self.config.thesis),
        )

        check_results = [] if self.options.skip_checks else await self._run_checks(artifact)
        judge_results, stability = await self._run_judges(artifact, context)

        self.options.progress("claims", "claim graph", "start")
        graph = build_graph(judge_results)
        coverage = graph.coverage()
        self.options.progress("claims", "claim graph", "ok")

        self.options.progress("meta", "meta review", "start")
        meta_provider = self._meta_provider()
        meta = await MetaJudge(meta_provider).review(
            judge_results,
            [*check_results, *_claim_findings_as_check(graph)],
        )
        self.options.progress("meta", "meta review", "ok")
        # Dispositions Veritas knows for certain override whatever a judge said.
        for item in meta.consolidated:
            item.finding = settle(item.finding)

        judge_errors = [result.judge for result in judge_results if result.status == "error"]
        gate = evaluate_gate(
            meta.findings, check_results, self.gate_policy(), coverage, judge_errors
        )
        self.options.progress("gate", gate.status, "ok" if gate.status == "PASS" else "warn")

        usage = _aggregate_usage(judge_results, meta, self.config.pricing)

        manifest = RunManifest(
            run_id=run_id,
            started_at=started,
            finished_at=datetime.now(UTC),
            veritas_version=__version__,
            profile=self.profile.name,
            profile_version=self.profile.version,
            artifact_id=artifact.id,
            artifact_type=artifact.type,
            artifact_paths=artifact.file_list(),
            artifact_commit=artifact.commit_sha(),
            config=self.config.model_dump(mode="json", exclude={"root"}),
            models=self._model_metadata(),
            prompt_versions=self.profile.prompt_versions(),
            judge_versions={judge.name: judge.version for judge in self.build_judges()},
            usage=usage.model_dump(mode="json"),
        )
        return EvaluationResult(
            manifest=manifest,
            judge_results=judge_results,
            check_results=check_results,
            claims=list(graph.claims.values()),
            evidence=list(graph.evidence.values()),
            coverage=coverage,
            meta_review=meta,
            gate=gate,
            stability=stability,
        )

    async def _run_checks(self, artifact: Artifact) -> list[CheckResult]:
        configs = self.check_configs()
        if not configs:
            return []
        semaphore = asyncio.Semaphore(max(1, self.config.concurrency))

        async def run_one(name: str, config: CheckConfig) -> CheckResult:
            async with semaphore:
                self.options.progress("check", name, "start")
                check = build_check(name, config, self.config.execution)
                result = await check.run(artifact)
                self.options.progress("check", name, "ok" if result.passed else "fail")
                return result

        return list(await asyncio.gather(*(run_one(name, cfg) for name, cfg in configs.items())))

    async def _run_judges(
        self, artifact: Artifact, context: EvaluationContext
    ) -> tuple[list[JudgeResult], list[JudgeStability]]:
        judges = self.build_judges()
        scoped = self._scoped_artifacts(artifact, judges)
        semaphore = asyncio.Semaphore(max(1, self.config.concurrency))
        repeats = max(1, self.options.runs)

        async def run_one(judge: LLMJudge, attempt: int) -> JudgeResult:
            async with semaphore:
                if attempt == 0:
                    self.options.progress("judge", judge.name, "start")
                # Judges are blind: each call sees only the artifact and the context.
                result = await judge.evaluate(scoped.get(judge.name, artifact), context)
                if attempt == 0:
                    self.options.progress(
                        "judge",
                        judge.name,
                        # A judge that broke is not a judge that found problems.
                        # Collapsing both into "fail" once let a degraded panel
                        # read as a thorough one.
                        {"pass": "ok", "warning": "warn", "error": "error"}.get(
                            result.status, "fail"
                        ),
                    )
                return result

        tasks = [run_one(judge, attempt) for judge in judges for attempt in range(repeats)]
        flat = list(await asyncio.gather(*tasks))

        if repeats == 1:
            return flat, []

        primary: list[JudgeResult] = []
        stability: list[JudgeStability] = []
        for index, judge in enumerate(judges):
            attempts = flat[index * repeats : (index + 1) * repeats]
            primary.append(_worst_result(attempts))
            stability.append(_stability_for(judge.name, attempts))
        return primary, stability

    def _scoped_artifacts(self, artifact: Artifact, judges: list[LLMJudge]) -> dict[str, Artifact]:
        """Give each judge named in ``judge_paths`` only the segments it needs.

        Segments come from the configured artifact, so a judge can never see
        more than artifact.paths offers; checks are unaffected and read it all.
        """
        wanted = self.config.judge_paths
        if not wanted:
            return {}
        known = {spec.name for spec in self.profile.definition.judges}
        unknown = sorted(set(wanted) - known)
        if unknown:
            raise ConfigError(
                f"judge_paths names unknown judge(s): {', '.join(unknown)}. "
                f"Profile '{self.profile.name}' defines: {', '.join(sorted(known))}"
            )
        segments = artifact.segments()
        scoped: dict[str, Artifact] = {}
        for judge in judges:
            prefixes = [item.strip("/").removeprefix("./") for item in wanted.get(judge.name, [])]
            if not prefixes:
                continue
            kept = [
                segment
                for segment in segments
                if any(
                    segment.path == prefix or segment.path.startswith(prefix + "/")
                    for prefix in prefixes
                )
            ]
            scoped[judge.name] = replace(artifact, _segments=kept)
        return scoped

    def _meta_provider(self) -> ModelProvider | None:
        role = self.profile.definition.meta_model_role
        if role not in self.config.models and "default" not in self.config.models:
            return None
        return self.provider_for(role)

    def _model_metadata(self) -> dict[str, Any]:
        return {
            role: model.model_dump(mode="json", exclude={"api_key_env"})
            for role, model in self.config.models.items()
        }


def _claim_findings_as_check(graph: ClaimGraph) -> Sequence[CheckResult]:
    """Feed unsupported claims into the meta review as a deterministic producer."""
    findings = claim_findings(graph)
    if not findings:
        return []
    return [
        CheckResult(
            check="claim-graph",
            status="fail",
            summary=f"{len(findings)} unsupported claim(s)",
            findings=findings,
        )
    ]


def _worst_result(attempts: list[JudgeResult]) -> JudgeResult:
    """Across repeated runs, keep the harshest verdict; never average them."""
    return max(
        attempts,
        key=lambda result: (
            severity_rank(max_severity([item.severity for item in result.findings])),
            len(result.findings),
        ),
    )


def _stability_for(judge: str, attempts: list[JudgeResult]) -> JudgeStability:
    worst = [max_severity([item.severity for item in result.findings]) for result in attempts]
    distinct = len(set(worst))
    if distinct == 1:
        rating = "HIGH"
    elif distinct == 2:
        rating = "MEDIUM"
    else:
        rating = "LOW"
    return JudgeStability(
        judge=judge,
        runs=len(attempts),
        statuses=[result.status for result in attempts],
        worst_severities=worst,
        stability=rating,  # type: ignore[arg-type]
    )


def _aggregate_usage(
    judge_results: list[JudgeResult],
    meta: MetaReview | None = None,
    pricing: PricingConfig | None = None,
) -> UsageSummary:
    """Fold every model call in the run into per-call, per-model and total usage.

    The MetaJudge's own call is one of them. It used to be dropped, which
    understated every run that consulted a meta model.
    """
    calls = [call_from_metadata(result.judge, result.metadata) for result in judge_results]
    if meta is not None:
        meta_call = _meta_usage(meta)
        if meta_call is not None:
            calls.append(meta_call)
    return summarize(calls, pricing)


def _meta_usage(meta: MetaReview) -> CallUsage | None:
    """The MetaJudge's call, or None when it ran deterministically."""
    usage = meta.metadata.get("meta_model_usage")
    if not isinstance(usage, dict):
        return None
    return call_from_metadata(
        META_JUDGE_LABEL,
        {
            "usage": usage,
            "model": meta.metadata.get("meta_model"),
            "provider": meta.metadata.get("meta_provider"),
        },
    )


def all_findings(result: EvaluationResult) -> list[Finding]:
    return result.meta_review.findings


def default_artifact_paths(profile: Profile, config: VeritasConfig) -> list[str]:
    return config.artifact.paths or profile.definition.default_paths


def resolve_root(path: Path) -> Path:
    return path.resolve()
