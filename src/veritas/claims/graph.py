"""ClaimGraph: claims, the evidence attached to them, and coverage.

Domain-neutral on purpose. A paper claims a latency reduction; an architecture
document claims a failover budget; an agent spec claims a tool is idempotent.
All three are claims with evidence that either exists or does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from veritas.models.claim import Claim, ClaimStatus, Evidence
from veritas.models.evaluation import ClaimCoverage

SUPPORTED_STATUSES = ("verified", "partially-supported", "unsupported", "unverified")


@dataclass(slots=True)
class ClaimGraph:
    """Claims linked to the evidence that would substantiate them."""

    claims: dict[str, Claim] = field(default_factory=dict)
    evidence: dict[str, Evidence] = field(default_factory=dict)

    def add_claim(self, claim: Claim) -> None:
        existing = self.claims.get(claim.id)
        if existing is None:
            self.claims[claim.id] = claim
            return
        merged_ids = list(dict.fromkeys([*existing.evidence_ids, *claim.evidence_ids]))
        self.claims[claim.id] = existing.model_copy(
            update={
                "evidence_ids": merged_ids,
                "status": _worse_status(existing.status, claim.status),
                "rationale": existing.rationale or claim.rationale,
            }
        )

    def add_evidence(self, item: Evidence) -> None:
        self.evidence.setdefault(item.id, item)

    def evidence_for(self, claim_id: str) -> list[Evidence]:
        claim = self.claims.get(claim_id)
        if claim is None:
            return []
        return [self.evidence[eid] for eid in claim.evidence_ids if eid in self.evidence]

    def resolve(self) -> None:
        """Reconcile each claim's status with the evidence actually attached."""
        for claim_id, claim in list(self.claims.items()):
            supporting = [item for item in self.evidence_for(claim_id) if item.supports]
            refuting = [item for item in self.evidence_for(claim_id) if not item.supports]
            status: ClaimStatus = claim.status
            if refuting and not supporting:
                status = "unsupported"
            elif not supporting and status in ("verified", "partially-supported"):
                # Never let a claim be called verified without evidence behind it.
                status = "unverified"
            elif supporting and refuting and status == "verified":
                status = "partially-supported"
            self.claims[claim_id] = claim.model_copy(update={"status": status})

    def coverage(self) -> ClaimCoverage:
        """Evidence coverage. Never the sole quality score — findings stay visible."""
        claims = list(self.claims.values())
        major = [claim for claim in claims if claim.importance == "major"]
        counts = dict.fromkeys(SUPPORTED_STATUSES, 0)
        for claim in claims:
            counts[claim.status] += 1
        major_verified = sum(1 for claim in major if claim.status == "verified")
        major_partial = sum(1 for claim in major if claim.status == "partially-supported")
        denominator = len(major)
        coverage = (
            round((major_verified + 0.5 * major_partial) / denominator * 100, 1)
            if denominator
            else 0.0
        )
        return ClaimCoverage(
            total=len(claims),
            major=len(major),
            verified=counts["verified"],
            partially_supported=counts["partially-supported"],
            unsupported=counts["unsupported"],
            unverified=counts["unverified"],
            coverage=coverage,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "claims": [claim.model_dump() for claim in self.claims.values()],
            "evidence": [item.model_dump() for item in self.evidence.values()],
            "coverage": self.coverage().model_dump(),
        }


_STATUS_SEVERITY = {
    "verified": 0,
    "partially-supported": 1,
    "unverified": 2,
    "unsupported": 3,
}


def _worse_status(left: str, right: str) -> ClaimStatus:
    worst = max(left, right, key=lambda item: _STATUS_SEVERITY.get(item, 2))
    return worst  # type: ignore[return-value]
