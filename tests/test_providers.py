"""Provider adapters, exercised against a real local HTTP endpoint."""

from __future__ import annotations

import json
from typing import Any

import pytest

from veritas.judges.llm import JudgeResponse
from veritas.providers import build_provider
from veritas.providers.base import (
    MissingCredentialsError,
    ModelSpec,
    ProviderError,
    json_schema_for,
    parse_structured,
)


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google")


def spec(provider: str, base_url: str, **kwargs: Any) -> ModelSpec:
    return ModelSpec(provider=provider, model="test-model", base_url=base_url, **kwargs)


# ----------------------------------------------------------------- schema


def test_json_schema_has_no_refs_or_defs() -> None:
    schema = json_schema_for(JudgeResponse)
    text = json.dumps(schema)
    assert "$ref" not in text
    assert "$defs" not in text
    assert schema["properties"]["findings"]["items"]["type"] == "object"


def test_parse_structured_accepts_fenced_json() -> None:
    parsed = parse_structured('```json\n{"status": "pass", "summary": "ok"}\n```', JudgeResponse)
    assert parsed.summary == "ok"


def test_parse_structured_accepts_json_embedded_in_prose() -> None:
    raw = 'Here is my review:\n{"status": "warning", "summary": "s"}\nThanks!'
    assert parse_structured(raw, JudgeResponse).status == "warning"


def test_parse_structured_rejects_unparseable_output() -> None:
    with pytest.raises(ProviderError):
        parse_structured("I would rather write an essay about this paper.", JudgeResponse)


# --------------------------------------------------------------- anthropic


def test_anthropic_sends_a_forced_tool_call_and_parses_it(serve, judge_payload) -> None:
    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        assert path == "/v1/messages"
        assert body["tool_choice"] == {"type": "tool", "name": "emit_evaluation"}
        assert body["system"].startswith("SECURITY RULES")
        return 200, {
            "model": "test-model",
            "content": [{"type": "tool_use", "name": "emit_evaluation", "input": judge_payload}],
            "usage": {"input_tokens": 1200, "output_tokens": 300},
        }

    with serve(handler) as server:
        provider = build_provider(spec("anthropic", server.base_url))
        response = _run(provider, "SECURITY RULES ...", "artifact")
    assert isinstance(response.value, JudgeResponse)
    assert len(response.value.findings) == 2
    assert response.usage["input_tokens"] == 1200


def test_anthropic_requires_its_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = build_provider(spec("anthropic", "http://127.0.0.1:1"))
    with pytest.raises(MissingCredentialsError, match="ANTHROPIC_API_KEY"):
        _run(provider, "s", "u")


# ------------------------------------------------------------------ openai


def test_openai_requests_a_json_schema_and_parses_the_message(serve, judge_payload) -> None:
    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        assert path == "/chat/completions"
        assert body["response_format"]["type"] == "json_schema"
        return 200, {
            "model": "test-model",
            "choices": [{"message": {"content": json.dumps(judge_payload)}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 120, "total_tokens": 1020},
        }

    with serve(handler) as server:
        response = _run(build_provider(spec("openai", server.base_url)), "s", "u")
    assert response.usage["total_tokens"] == 1020


def test_openai_falls_back_when_the_endpoint_rejects_json_schema(serve, judge_payload) -> None:
    """Self-hosted OpenAI-compatible servers often support only json_object."""
    seen: list[str] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        mode = body["response_format"]["type"]
        seen.append(mode)
        if mode == "json_schema":
            return 400, {"error": {"message": "response_format json_schema is not supported"}}
        return 200, {"choices": [{"message": {"content": json.dumps(judge_payload)}}]}

    with serve(handler) as server:
        response = _run(build_provider(spec("openai", server.base_url, max_retries=1)), "s", "u")
    assert seen == ["json_schema", "json_object"]
    assert isinstance(response.value, JudgeResponse)


def test_openai_compatible_uses_its_own_key_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    provider = build_provider(spec("openai-compatible", "http://127.0.0.1:1"))
    with pytest.raises(MissingCredentialsError, match="OPENAI_COMPATIBLE_API_KEY"):
        _run(provider, "s", "u")


def test_transient_errors_are_retried(serve, judge_payload) -> None:
    attempts: list[int] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        attempts.append(1)
        if len(attempts) < 3:
            return 503, {"error": {"message": "overloaded"}}
        return 200, {"choices": [{"message": {"content": json.dumps(judge_payload)}}]}

    with serve(handler) as server:
        _run(build_provider(spec("openai", server.base_url, max_retries=3)), "s", "u")
    assert len(attempts) == 3


def test_permanent_errors_are_not_retried(serve) -> None:
    attempts: list[int] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        attempts.append(1)
        return 401, {"error": {"message": "invalid api key"}}

    with serve(handler) as server, pytest.raises(ProviderError, match="401"):
        _run(build_provider(spec("openai", server.base_url, max_retries=3)), "s", "u")
    assert len(attempts) == 1


# ------------------------------------------------------------------ google


def test_google_sends_a_response_schema_and_parses_the_text(serve, judge_payload) -> None:
    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        assert path == "/models/test-model:generateContent"
        config = body["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["responseSchema"]["type"] == "OBJECT"
        assert "additionalProperties" not in json.dumps(config["responseSchema"])
        return 200, {
            "modelVersion": "test-model",
            "candidates": [{"content": {"parts": [{"text": json.dumps(judge_payload)}]}}],
            "usageMetadata": {"promptTokenCount": 800, "candidatesTokenCount": 90},
        }

    with serve(handler) as server:
        response = _run(build_provider(spec("google", server.base_url)), "s", "u")
    assert response.usage["input_tokens"] == 800


def test_google_reports_a_blocked_response_as_a_provider_error(serve) -> None:
    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, {"promptFeedback": {"blockReason": "SAFETY"}}

    with serve(handler) as server, pytest.raises(ProviderError, match="no candidates"):
        _run(build_provider(spec("google", server.base_url)), "s", "u")


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ProviderError, match="unknown provider"):
        build_provider(ModelSpec(provider="nope", model="m"))


def _run(provider, system: str, user: str):
    import asyncio

    return asyncio.run(provider.generate_structured(system, user, JudgeResponse))
