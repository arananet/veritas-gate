"""OpenAI Chat Completions adapter.

Also serves any OpenAI-compatible endpoint (vLLM, Ollama, LM Studio, Together,
Groq, Azure-style gateways) by pointing ``base_url`` at it. Structured output
uses ``response_format: json_schema`` where supported and falls back to
``json_object`` when the endpoint rejects it.
"""

from __future__ import annotations

from typing import Any

from veritas.providers.base import (
    ModelSpec,
    ProviderError,
    SchemaT,
    StructuredResponse,
    json_schema_for,
    parse_structured,
)
from veritas.providers.http import post_json, require_api_key

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider:
    """Provider adapter for OpenAI and OpenAI-compatible endpoints."""

    name = "openai"
    default_api_key_env = "OPENAI_API_KEY"

    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[SchemaT],
    ) -> StructuredResponse:
        base = (self.spec.base_url or DEFAULT_BASE_URL).rstrip("/")
        headers = {
            "authorization": f"Bearer {require_api_key(self.spec, self.default_api_key_env)}",
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.spec.model,
            "max_tokens": self.spec.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": json_schema_for(schema),
                },
            },
        }
        if self.spec.temperature is not None:
            payload["temperature"] = self.spec.temperature
        payload.update(self.spec.extra)

        url = f"{base}/chat/completions"
        try:
            body = await post_json(self.spec, url, headers=headers, payload=payload)
        except ProviderError as exc:
            if not _rejects_json_schema(exc):
                raise
            payload["response_format"] = {"type": "json_object"}
            body = await post_json(self.spec, url, headers=headers, payload=payload)

        raw = _extract_message(body)
        value = parse_structured(raw, schema)
        return StructuredResponse(value=value, usage=_usage(body), raw=raw)


class OpenAICompatibleProvider(OpenAIProvider):
    """Same wire protocol, different key variable, for self-hosted gateways."""

    name = "openai-compatible"
    default_api_key_env = "OPENAI_COMPATIBLE_API_KEY"


def _rejects_json_schema(exc: ProviderError) -> bool:
    text = str(exc).lower()
    return "json_schema" in text or "response_format" in text


def _extract_message(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("openai response contained no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ProviderError("openai response contained no message")
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("openai response message was empty")
    return content


def _usage(body: dict[str, Any]) -> dict[str, Any]:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "model": body.get("model"),
    }
