"""Token and cost accounting."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from veritas.config import ModelPrice, PricingConfig, VeritasConfig
from veritas.usage import CallUsage, UsageSummary, call_from_metadata, summarize


def call(label: str, model: str | None, inp: int | None, out: int | None) -> CallUsage:
    return CallUsage(label=label, model=model, input_tokens=inp, output_tokens=out)


def test_totals_equal_the_sum_of_their_parts() -> None:
    summary = summarize([call("a", "m1", 100, 10), call("b", "m2", 200, 20)])
    assert summary.calls == 2
    assert summary.input_tokens == 300
    assert summary.output_tokens == 30
    assert sum(entry.input_tokens for entry in summary.by_model) == summary.input_tokens
    assert sum(entry.output_tokens for entry in summary.by_model) == summary.output_tokens


def test_two_judges_on_one_model_aggregate_under_it_once() -> None:
    summary = summarize([call("a", "shared", 100, 10), call("b", "shared", 50, 5)])
    assert [entry.model for entry in summary.by_model] == ["shared"]
    entry = summary.by_model[0]
    assert entry.calls == 2
    assert entry.input_tokens == 150
    assert entry.total_tokens == 165


def test_a_call_without_usage_metadata_is_counted_but_not_as_zero() -> None:
    summary = summarize([call("a", "m1", 100, 10), call("b", "m1", None, None)])
    assert summary.calls == 2
    assert summary.calls_without_usage == 1
    # The tokens we do know about are still reported.
    assert summary.input_tokens == 100


def test_no_pricing_means_no_cost_at_all() -> None:
    summary = summarize([call("a", "m1", 1_000_000, 0)])
    assert summary.cost is None
    assert summary.has_cost is False
    assert summary.currency is None


def test_cost_uses_input_and_output_rates_separately() -> None:
    pricing = PricingConfig(
        rates={"m1": ModelPrice(input_per_million=3.0, output_per_million=15.0)}
    )
    summary = summarize([call("a", "m1", 1_000_000, 1_000_000)], pricing)
    assert summary.cost == pytest.approx(18.0)
    assert summary.currency == "USD"
    assert summary.cost_partial is False


def test_an_unpriced_model_marks_the_summary_partial_and_is_named() -> None:
    pricing = PricingConfig(
        rates={"priced": ModelPrice(input_per_million=1.0, output_per_million=1.0)}
    )
    summary = summarize(
        [call("a", "priced", 1_000_000, 0), call("b", "unpriced", 5_000_000, 0)], pricing
    )
    assert summary.cost_partial is True
    assert summary.unpriced_models == ["unpriced"]
    # The priced half is still reported; an unpriced model is never free.
    assert summary.cost == pytest.approx(1.0)


def test_a_missing_usage_payload_also_makes_the_cost_partial() -> None:
    pricing = PricingConfig(rates={"m1": ModelPrice(input_per_million=1.0)})
    summary = summarize([call("a", "m1", 1_000_000, 0), call("b", "m1", None, None)], pricing)
    assert summary.cost_partial is True


def test_a_negative_rate_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelPrice(input_per_million=-1.0)
    with pytest.raises(ValidationError):
        ModelPrice(output_per_million=-0.5)


def test_pricing_loads_from_configuration() -> None:
    config = VeritasConfig.model_validate(
        {
            "pricing": {
                "currency": "EUR",
                "rates": {"claude-x": {"input_per_million": 3, "output_per_million": 15}},
            }
        }
    )
    assert config.pricing.currency == "EUR"
    assert config.pricing.rates["claude-x"].output_per_million == 15


def test_call_from_metadata_reads_a_judge_result() -> None:
    usage = call_from_metadata(
        "methodology",
        {
            "model": "claude-x",
            "provider": "anthropic",
            "usage": {"input_tokens": 12, "output_tokens": 3},
        },
    )
    assert usage.model == "claude-x"
    assert usage.provider == "anthropic"
    assert usage.total_tokens == 15


def test_call_from_metadata_survives_a_result_with_no_usage() -> None:
    usage = call_from_metadata("methodology", {"model": "claude-x"})
    assert usage.input_tokens is None
    assert usage.total_tokens is None


def test_a_summary_round_trips_through_the_manifest() -> None:
    summary = summarize([call("a", "m1", 10, 1)])
    restored = UsageSummary.model_validate(summary.model_dump(mode="json"))
    assert restored.by_model[0].model == "m1"
    assert restored.calls == 1


def test_the_meta_judge_call_is_counted_and_attributed_to_itself() -> None:
    """It used to be dropped, which understated every run with a meta model."""
    from veritas.engine import _aggregate_usage
    from veritas.models.evaluation import JudgeResult, MetaReview

    judge = JudgeResult(
        judge="methodology",
        status="pass",
        summary="",
        metadata={"model": "m1", "usage": {"input_tokens": 100, "output_tokens": 10}},
    )
    meta = MetaReview(
        summary="",
        metadata={
            "meta_model": "m2",
            "meta_provider": "anthropic",
            "meta_model_usage": {"input_tokens": 50, "output_tokens": 5},
        },
    )
    summary = _aggregate_usage([judge], meta)
    assert summary.calls == 2
    assert summary.input_tokens == 150
    assert {entry.model for entry in summary.by_model} == {"m1", "m2"}
    assert any(item.label == "meta-judge" for item in summary.by_call)


def test_a_deterministic_meta_review_adds_no_call() -> None:
    from veritas.engine import _aggregate_usage
    from veritas.models.evaluation import JudgeResult, MetaReview

    judge = JudgeResult(judge="methodology", status="pass", summary="", metadata={})
    summary = _aggregate_usage([judge], MetaReview(summary=""))
    assert summary.calls == 1
