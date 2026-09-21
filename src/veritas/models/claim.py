"""Claim graph primitives.

Deliberately domain-neutral: a claim is any assertion an artifact makes, and
evidence is anything that would substantiate it. Papers, architecture documents
and agent specifications all make claims.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ClaimStatus = Literal["verified", "partially-supported", "unsupported", "unverified"]


class Evidence(BaseModel):
    """A pointer to something that supports (or fails to support) a claim."""

    model_config = ConfigDict(extra="ignore")

    id: str
    description: str
    location: str | None = None
    artifact_path: str | None = None
    supports: bool = True


class Claim(BaseModel):
    """An assertion extracted from an artifact."""

    model_config = ConfigDict(extra="ignore")

    id: str
    text: str
    source_location: str | None = None
    importance: Literal["major", "minor"] = "major"
    evidence_ids: list[str] = Field(default_factory=list)
    status: ClaimStatus = "unverified"
    rationale: str | None = None
