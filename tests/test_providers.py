"""Provider adapters, exercised against a real local HTTP endpoint."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

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


# --------------------------------------------------------------------- TLS


def test_tls_verification_is_on_by_default() -> None:
    from veritas.providers.http import build_verify

    assert build_verify(ModelSpec(provider="anthropic", model="m")) is True


def test_a_ca_bundle_path_is_passed_through(tmp_path) -> None:
    """The right fix behind an intercepting proxy: trust its CA, keep checking."""
    from veritas.providers.http import build_verify

    bundle = tmp_path / "corp-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    spec = ModelSpec(provider="anthropic", model="m", tls_verify=str(bundle))
    assert build_verify(spec) == str(bundle)


def test_a_missing_ca_bundle_is_reported_clearly(tmp_path) -> None:
    from veritas.providers.http import TLSConfigurationError, build_verify

    spec = ModelSpec(provider="anthropic", model="m", tls_verify=str(tmp_path / "absent.pem"))
    with pytest.raises(TLSConfigurationError, match="does not exist"):
        build_verify(spec)


def test_truststore_uses_the_system_trust_store() -> None:
    import ssl

    from veritas.providers.http import build_verify

    context = build_verify(ModelSpec(provider="anthropic", model="m", tls_verify="truststore"))
    assert isinstance(context, ssl.SSLContext)


def test_disabling_verification_warns_and_names_the_exposure(capsys) -> None:
    """Turning verification off must never be quiet."""
    from veritas.providers import http
    from veritas.providers.http import build_verify

    http._INSECURE_WARNED.clear()
    spec = ModelSpec(
        provider="anthropic", model="m", tls_verify=False, base_url="https://api.anthropic.com"
    )
    assert build_verify(spec) is False
    captured = capsys.readouterr().err
    assert "verification is disabled" in captured
    assert "NOT a local endpoint" in captured


def test_a_local_endpoint_gets_a_softer_warning(capsys) -> None:
    from veritas.providers import http
    from veritas.providers.http import build_verify

    http._INSECURE_WARNED.clear()
    spec = ModelSpec(
        provider="openai-compatible",
        model="m",
        tls_verify=False,
        base_url="https://localhost:8443/v1",
    )
    build_verify(spec)
    assert "This is a local endpoint" in capsys.readouterr().err


def test_an_unsupported_tls_value_is_rejected() -> None:
    from veritas.providers.http import TLSConfigurationError, build_verify

    with pytest.raises(TLSConfigurationError, match="unsupported value"):
        build_verify(ModelSpec(provider="anthropic", model="m", tls_verify=42))  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on", "default"])
def test_string_booleans_from_env_interpolation_enable_verification(value: str) -> None:
    """Regression: `tls_verify: ${VAR:-true}` was read as a path named "true"."""
    from veritas.providers.http import build_verify

    assert build_verify(ModelSpec(provider="anthropic", model="m", tls_verify=value)) is True


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "off"])
def test_string_booleans_from_env_interpolation_can_disable_verification(
    value: str, capsys
) -> None:
    from veritas.providers import http
    from veritas.providers.http import build_verify

    http._INSECURE_WARNED.clear()
    assert build_verify(ModelSpec(provider="anthropic", model="m", tls_verify=value)) is False
    assert "verification is disabled" in capsys.readouterr().err


def test_temperature_is_omitted_unless_it_is_configured(serve, judge_payload) -> None:
    """Regression: current Claude models return 400 for a `temperature` field."""
    seen: list[dict[str, Any]] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seen.append(body)
        return 200, {
            "content": [{"type": "tool_use", "name": "emit_evaluation", "input": judge_payload}]
        }

    with serve(handler) as server:
        _run(build_provider(spec("anthropic", server.base_url)), "s", "u")
    assert "temperature" not in seen[0]


def test_temperature_is_sent_when_it_is_configured(serve, judge_payload) -> None:
    seen: list[dict[str, Any]] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seen.append(body)
        return 200, {
            "content": [{"type": "tool_use", "name": "emit_evaluation", "input": judge_payload}]
        }

    with serve(handler) as server:
        _run(build_provider(spec("anthropic", server.base_url, temperature=0.2)), "s", "u")
    assert seen[0]["temperature"] == 0.2


def test_openai_omits_temperature_by_default(serve, judge_payload) -> None:
    seen: list[dict[str, Any]] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seen.append(body)
        return 200, {"choices": [{"message": {"content": json.dumps(judge_payload)}}]}

    with serve(handler) as server:
        _run(build_provider(spec("openai", server.base_url)), "s", "u")
    assert "temperature" not in seen[0]


def test_google_omits_temperature_by_default(serve, judge_payload) -> None:
    seen: list[dict[str, Any]] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seen.append(body)
        return 200, {"candidates": [{"content": {"parts": [{"text": json.dumps(judge_payload)}]}}]}

    with serve(handler) as server:
        _run(build_provider(spec("google", server.base_url)), "s", "u")
    assert "temperature" not in seen[0]["generationConfig"]


def test_an_exhausted_quota_is_not_retried(serve) -> None:
    """A 429 for an empty balance will not clear during a backoff."""
    attempts: list[int] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        attempts.append(1)
        return 429, {
            "error": {
                "message": "You have no credits remaining. Add credits to continue.",
                "code": "credit_balance_exhausted",
            }
        }

    with serve(handler) as server, pytest.raises(ProviderError, match="429"):
        _run(build_provider(spec("openai", server.base_url, max_retries=3)), "s", "u")
    assert len(attempts) == 1, "an exhausted balance must fail immediately"


def test_an_ordinary_rate_limit_is_still_retried(serve, judge_payload) -> None:
    attempts: list[int] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        attempts.append(1)
        if len(attempts) < 2:
            return 429, {"error": {"message": "rate limit exceeded, please slow down"}}
        return 200, {"choices": [{"message": {"content": json.dumps(judge_payload)}}]}

    with serve(handler) as server:
        _run(build_provider(spec("openai", server.base_url, max_retries=3)), "s", "u")
    assert len(attempts) == 2


def test_openai_sends_max_completion_tokens(serve, judge_payload) -> None:
    """OpenAI's newer models reject `max_tokens`."""
    seen: list[dict[str, Any]] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seen.append(body)
        return 200, {"choices": [{"message": {"content": json.dumps(judge_payload)}}]}

    with serve(handler) as server:
        _run(build_provider(spec("openai", server.base_url)), "s", "u")
    assert seen[0]["max_completion_tokens"] == 16000
    assert "max_tokens" not in seen[0]


def test_openai_compatible_sends_max_tokens(serve, judge_payload) -> None:
    """Self-hosted servers mostly only know the older field."""
    import os

    os.environ["OPENAI_COMPATIBLE_API_KEY"] = "test"
    seen: list[dict[str, Any]] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seen.append(body)
        return 200, {"choices": [{"message": {"content": json.dumps(judge_payload)}}]}

    with serve(handler) as server:
        _run(build_provider(spec("openai-compatible", server.base_url)), "s", "u")
    assert seen[0]["max_tokens"] == 16000
    assert "max_completion_tokens" not in seen[0]


def test_the_token_field_is_swapped_when_the_endpoint_rejects_it(serve, judge_payload) -> None:
    """Regression: a 400 naming max_completion_tokens killed the whole judge."""
    seen: list[dict[str, Any]] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        seen.append(body)
        if "max_completion_tokens" in body:
            return 400, {
                "error": {
                    "message": (
                        "Unsupported parameter: 'max_completion_tokens' is not supported "
                        "with this model. Use 'max_tokens' instead."
                    ),
                    "code": "unsupported_parameter",
                }
            }
        return 200, {"choices": [{"message": {"content": json.dumps(judge_payload)}}]}

    with serve(handler) as server:
        _run(build_provider(spec("openai", server.base_url, max_retries=1)), "s", "u")
    assert len(seen) == 2
    assert seen[1]["max_tokens"] == 16000


def test_each_fallback_is_attempted_only_once(serve) -> None:
    """A genuine failure must surface, not loop through fallbacks forever."""
    attempts: list[int] = []

    def handler(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        attempts.append(1)
        return 400, {"error": {"message": "max_tokens and response_format are both wrong"}}

    with serve(handler) as server, pytest.raises(ProviderError, match="400"):
        _run(build_provider(spec("openai", server.base_url, max_retries=1)), "s", "u")
    # One original attempt plus one of each fallback, then the error is raised.
    assert len(attempts) == 3


# ------------------------------------------- a field sent as a JSON string


class _Schema(BaseModel):
    summary: str = ""
    findings: list[dict] = []
    meta: dict = {}


def test_a_list_field_sent_as_a_json_string_is_decoded() -> None:
    """A judge returned its whole findings array inside quotes.

    The parser rejected it, the judge was recorded as an error, and an
    evaluation costing ten dollars came back a panel member short.
    """
    text = json.dumps({"summary": "ok", "findings": json.dumps([{"title": "a problem"}])})
    parsed = parse_structured(text, _Schema)
    assert parsed.findings == [{"title": "a problem"}]


def test_an_object_field_sent_as_a_json_string_is_decoded() -> None:
    text = json.dumps({"summary": "ok", "meta": json.dumps({"model": "x"})})
    parsed = parse_structured(text, _Schema)
    assert parsed.meta == {"model": "x"}


def test_a_genuine_string_field_containing_json_is_left_alone() -> None:
    """The schema decides, so the parser never guesses what a value meant."""
    text = json.dumps({"summary": '{"not": "a structure"}'})
    parsed = parse_structured(text, _Schema)
    assert parsed.summary == '{"not": "a structure"}'


def test_a_string_field_with_invalid_json_is_left_alone() -> None:
    text = json.dumps({"summary": "ok", "findings": "not json at all"})
    with pytest.raises(ProviderError):
        parse_structured(text, _Schema)


def test_a_well_formed_response_is_unaffected() -> None:
    text = json.dumps({"summary": "ok", "findings": [{"title": "a"}], "meta": {"k": "v"}})
    parsed = parse_structured(text, _Schema)
    assert parsed.findings == [{"title": "a"}]
    assert parsed.meta == {"k": "v"}


def test_a_decoded_field_of_the_wrong_shape_still_fails() -> None:
    """Leniency is about transport, never about content."""
    text = json.dumps({"summary": "ok", "findings": json.dumps("a bare string")})
    with pytest.raises(ProviderError) as exc:
        parse_structured(text, _Schema)
    assert "_Schema" in str(exc.value)


# ------------------------------------------- honouring a stated wait


def test_a_429_body_saying_try_again_in_seconds_is_honoured() -> None:
    """The old backoff capped at 8s and spent every attempt before a TPM limit cleared."""
    from veritas.providers.http import _stated_wait

    body = "Rate limit reached ... Please try again in 33.024s. Visit ..."
    wait = _stated_wait({}, body)
    assert wait is not None and wait >= 33.0


def test_a_wait_in_milliseconds_is_honoured() -> None:
    from veritas.providers.http import _stated_wait

    wait = _stated_wait({}, "Please try again in 500ms.")
    assert wait is not None and 0.5 <= wait < 3


def test_a_retry_after_header_is_honoured() -> None:
    from veritas.providers.http import _stated_wait

    assert (_stated_wait({"retry-after": "12"}, "") or 0) >= 12


def test_a_stated_wait_is_capped() -> None:
    from veritas.providers.http import MAX_STATED_WAIT, _stated_wait

    assert _stated_wait({}, "try again in 9999s") == MAX_STATED_WAIT


def test_no_stated_wait_leaves_the_backoff_in_charge() -> None:
    from veritas.providers.http import _stated_wait

    assert _stated_wait({}, "internal server error") is None
