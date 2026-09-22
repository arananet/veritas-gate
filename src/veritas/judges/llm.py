"""The declarative LLM judge.

Every judge in every profile is an instance of this class: its behaviour comes
entirely from a prompt file and a model role. Adding a judge is a YAML + prompt
change, never a Python change.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from veritas.artifacts.base import Artifact
from veritas.judges.base import EvaluationContext, error_result
from veritas.models.claim import Claim, Evidence
from veritas.models.evaluation import JudgeResult, JudgeStatus
from veritas.models.finding import Finding
from veritas.providers.base import ModelProvider, ProviderError
from veritas.security import EVALUATION_RULES, UNTRUSTED_PREAMBLE, wrap_untrusted

NO_EVIDENCE_PENALTY = 0.6


class RawFinding(BaseModel):
    """The finding shape asked of the model (ids are assigned by us, not by it)."""

    model_config = ConfigDict(extra="ignore")

    title: str
    severity: str = "minor"
    category: str = "general"
    description: str
    location: str | None = None
    evidence: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    confidence: float = 0.5


class RawClaim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    source_location: str | None = None
    importance: str = "major"
    status: str = "unverified"
    evidence: list[str] = Field(default_factory=list)
    rationale: str | None = None


class JudgeResponse(BaseModel):
    """The structured schema every judge model must return."""

    model_config = ConfigDict(extra="ignore")

    status: str = "pass"
    summary: str = ""
    findings: list[RawFinding] = Field(default_factory=list)
    claims: list[RawClaim] = Field(default_factory=list)


_VALID_SEVERITIES = {"info", "minor", "major", "critical"}
_VALID_STATUSES = {"pass", "fail", "warning"}
_VALID_CLAIM_STATUSES = {"verified", "partially-supported", "unsupported", "unverified"}


class LLMJudge:
    """A judge whose behaviour is defined by a versioned prompt file."""

    def __init__(
        self,
        *,
        name: str,
        prompt: str,
        provider: ModelProvider,
        version: str = "1",
        category_prefix: str | None = None,
        extracts_claims: bool = False,
        model_role: str = "default",
    ) -> None:
        self.name = name
        self.prompt = prompt
        self.provider = provider
        self.version = version
        self.category_prefix = category_prefix or _prefix_from_name(name)
        self.extracts_claims = extracts_claims
        self.model_role = model_role

    def system_prompt(self, context: EvaluationContext) -> str:
        parts = [
            UNTRUSTED_PREAMBLE,
            EVALUATION_RULES,
            self.prompt.strip(),
            (
                f"Profile: {context.profile} (version {context.profile_version}). "
                f"Artifact type: {context.artifact_type}."
            ),
        ]
        if context.rubric:
            parts.append(f"Rubric (authoritative, from the profile): {context.rubric}")
        return "\n\n".join(part for part in parts if part.strip())

    def user_prompt(self, artifact: Artifact) -> str:
        body = wrap_untrusted(artifact.segments(), per_file_limit=artifact.max_file_chars)
        listing = "\n".join(f"- {path}" for path in artifact.file_list()) or "- (none)"
        return (
            f"Artifact id: {artifact.id}\nArtifact type: {artifact.type}\n"
            f"Files provided:\n{listing}\n\n"
            f"{body}\n\n"
            "Evaluate the artifact above and return the required structured result. "
            "Cite concrete locations (file path, section, line or table) in every finding."
        )

    async def evaluate(self, artifact: Artifact, context: EvaluationContext) -> JudgeResult:
        try:
            response = await self.provider.generate_structured(
                self.system_prompt(context),
                self.user_prompt(artifact),
                JudgeResponse,
            )
        except ProviderError as exc:
            return error_result(self.name, str(exc))

        payload = response.value
        assert isinstance(payload, JudgeResponse)
        findings = self._build_findings(payload)
        claims, evidence = self._build_claims(payload)
        return JudgeResult(
            judge=self.name,
            status=_coerce_status(payload.status, findings),
            summary=payload.summary.strip(),
            findings=findings,
            claims=claims,
            evidence=evidence,
            metadata={
                "judge_version": self.version,
                "model_role": self.model_role,
                "model": _model_id(self.provider),
                "provider": getattr(self.provider, "name", "unknown"),
                "usage": response.usage,
            },
        )

    def _build_findings(self, payload: JudgeResponse) -> list[Finding]:
        findings: list[Finding] = []
        for index, raw in enumerate(payload.findings, start=1):
            severity = raw.severity.strip().lower()
            if severity not in _VALID_SEVERITIES:
                severity = "minor"
            evidence = [item.strip() for item in raw.evidence if item and item.strip()]
            finding = Finding(
                id=f"{self.category_prefix}-{index:03d}",
                title=raw.title.strip() or "Untitled finding",
                severity=severity,  # type: ignore[arg-type]
                category=raw.category.strip() or self.name,
                description=raw.description.strip(),
                location=raw.location,
                evidence=evidence,
                recommendation=raw.recommendation,
                confidence=max(0.0, min(1.0, raw.confidence)),
                source=self.name,
            )
            # Evidence-first: an unevidenced finding survives but carries less weight.
            if not finding.has_evidence:
                finding = finding.discounted(NO_EVIDENCE_PENALTY)
            findings.append(finding)
        return findings

    def _build_claims(self, payload: JudgeResponse) -> tuple[list[Claim], list[Evidence]]:
        if not self.extracts_claims:
            return [], []
        claims: list[Claim] = []
        evidence: list[Evidence] = []
        for index, raw in enumerate(payload.claims, start=1):
            claim_id = f"{self.category_prefix}-CLAIM-{index:03d}"
            evidence_ids: list[str] = []
            for position, item in enumerate(raw.evidence, start=1):
                text = item.strip()
                if not text:
                    continue
                evidence_id = f"{claim_id}-E{position}"
                evidence_ids.append(evidence_id)
                evidence.append(Evidence(id=evidence_id, description=text))
            status = raw.status.strip().lower()
            if status not in _VALID_CLAIM_STATUSES:
                status = "unverified"
            # A claim with no evidence attached cannot be called verified.
            if not evidence_ids and status == "verified":
                status = "unverified"
            claims.append(
                Claim(
                    id=claim_id,
                    text=raw.text.strip(),
                    source_location=raw.source_location,
                    importance="minor" if raw.importance.strip().lower() == "minor" else "major",
                    evidence_ids=evidence_ids,
                    status=status,  # type: ignore[arg-type]
                    rationale=raw.rationale,
                )
            )
        return claims, evidence


def _coerce_status(status: str, findings: list[Finding]) -> JudgeStatus:
    """Trust severities over the model's self-reported status."""
    worst = {finding.severity for finding in findings}
    if "critical" in worst or "major" in worst:
        return "fail"
    normalized = status.strip().lower()
    if normalized in _VALID_STATUSES:
        if normalized == "pass" and "minor" in worst:
            return "warning"
        return normalized  # type: ignore[return-value]
    return "warning" if worst else "pass"


def _prefix_from_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() else "-" for char in name.upper())
    return cleaned.strip("-").replace("--", "-") or "JUDGE"


def judge_metadata(judge: LLMJudge) -> dict[str, Any]:
    return {"name": judge.name, "version": judge.version, "model_role": judge.model_role}


def _model_id(provider: ModelProvider | None) -> str | None:
    """The concrete model a provider runs, for per-model usage accounting.

    A blind panel deliberately runs judges on different models, so a role alone
    cannot attribute tokens or cost.
    """
    spec = getattr(provider, "spec", None)
    model = getattr(spec, "model", None)
    return model if isinstance(model, str) and model else None
