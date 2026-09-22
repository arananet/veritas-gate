"""Token and cost accounting for one evaluation.

Tokens are always recorded; they come back from every provider. Cost is only
produced when the operator supplies rates, because prices depend on the model,
change without notice, and differ by provider and tier. A rate compiled into
Veritas would go stale and then be frozen into an immutable run record, which
is worse than reporting no cost at all.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from veritas.config import PricingConfig

META_JUDGE_LABEL = "meta-judge"


class CallUsage(BaseModel):
    """What one model call consumed."""

    model_config = ConfigDict(extra="ignore")

    label: str
    model: str | None = None
    provider: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class ModelUsage(BaseModel):
    """Everything one model consumed across the calls that used it."""

    model_config = ConfigDict(extra="ignore")

    model: str
    provider: str | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageSummary(BaseModel):
    """Per-call, per-model and total accounting for one evaluation."""

    model_config = ConfigDict(extra="ignore")

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_call: list[CallUsage] = Field(default_factory=list)
    by_model: list[ModelUsage] = Field(default_factory=list)
    cost: float | None = None
    currency: str | None = None
    # True when at least one model that ran has no configured price. The cost
    # above then covers only part of the run and understates the real total.
    cost_partial: bool = False
    unpriced_models: list[str] = Field(default_factory=list)
    # Calls whose provider returned no usage metadata. They are counted as
    # calls but contribute no tokens, so the totals are a floor, not a truth.
    calls_without_usage: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def has_cost(self) -> bool:
        return self.cost is not None


def _tokens(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def summarize(calls: list[CallUsage], pricing: PricingConfig | None = None) -> UsageSummary:
    """Fold individual calls into per-model and total accounting."""
    summary = UsageSummary(calls=len(calls))
    models: dict[str, ModelUsage] = {}

    for call in calls:
        if call.input_tokens is None and call.output_tokens is None:
            summary.calls_without_usage += 1
        summary.input_tokens += call.input_tokens or 0
        summary.output_tokens += call.output_tokens or 0
        if not call.model:
            continue
        entry = models.setdefault(call.model, ModelUsage(model=call.model, provider=call.provider))
        entry.calls += 1
        entry.input_tokens += call.input_tokens or 0
        entry.output_tokens += call.output_tokens or 0

    summary.by_call = calls
    summary.by_model = sorted(models.values(), key=lambda item: item.model)

    if pricing is not None and pricing.rates:
        _apply_pricing(summary, pricing)
    return summary


def _apply_pricing(summary: UsageSummary, pricing: PricingConfig) -> None:
    summary.currency = pricing.currency
    total = 0.0
    priced_any = False

    for entry in summary.by_model:
        rate = pricing.rates.get(entry.model)
        if rate is None:
            summary.cost_partial = True
            summary.unpriced_models.append(entry.model)
            continue
        entry.cost = rate.cost_for(entry.input_tokens, entry.output_tokens)
        total += entry.cost
        priced_any = True

    for call in summary.by_call:
        rate = pricing.rates.get(call.model or "")
        if rate is not None:
            call.cost = rate.cost_for(call.input_tokens or 0, call.output_tokens or 0)

    # A model that ran but was never attributed (no usage metadata at all) is
    # invisible to pricing; say nothing rather than report a confident zero.
    summary.cost = round(total, 6) if priced_any else None
    if summary.calls_without_usage:
        summary.cost_partial = True


def call_from_metadata(label: str, metadata: dict[str, Any]) -> CallUsage:
    """Read one judge result's metadata into a usage record."""
    usage = metadata.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return CallUsage(
        label=label,
        model=metadata.get("model") or None,
        provider=metadata.get("provider") or None,
        input_tokens=_tokens(usage, "input_tokens"),
        output_tokens=_tokens(usage, "output_tokens"),
    )
