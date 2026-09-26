"""What a read-only investigation established before any repair."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Fact(BaseModel):
    """One statement the investigator verified, with where it came from."""

    model_config = ConfigDict(extra="ignore")

    statement: str
    # A file path with line, or the exact command run and what it printed.
    source: str
    action_id: str | None = None
    verified: bool = True


class Investigation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = "completed"
    facts: list[Fact] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    reverted: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def verified_facts(self) -> list[Fact]:
        """Facts with a stated source; an unsourced statement is not a fact."""
        return [fact for fact in self.facts if fact.verified and fact.source.strip()]
