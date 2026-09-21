"""Anthropic Messages API adapter.

Structured output is obtained with a single forced tool call, which is the
supported way to constrain Anthropic responses to a JSON schema.
"""

from __future__ import annotations

import json
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

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
TOOL_NAME = "emit_evaluation"


class AnthropicProvider:
    """Provider adapter for Anthropic models."""

    name = "anthropic"

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
            "x-api-key": require_api_key(self.spec, "ANTHROPIC_API_KEY"),
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.spec.model,
            "max_tokens": self.spec.max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "tools": [
                {
                    "name": TOOL_NAME,
                    "description": "Return the evaluation result as structured data.",
                    "input_schema": json_schema_for(schema),
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }
        if self.spec.temperature is not None:
            payload["temperature"] = self.spec.temperature
        payload.update(self.spec.extra)

        body = await post_json(self.spec, f"{base}/v1/messages", headers=headers, payload=payload)
        raw = _extract_tool_input(body)
        value = parse_structured(raw, schema)
        return StructuredResponse(value=value, usage=_usage(body), raw=raw)


def _extract_tool_input(body: dict[str, Any]) -> str:
    content = body.get("content")
    if not isinstance(content, list):
        raise ProviderError("anthropic response contained no content blocks")
    texts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use":
            return json.dumps(block.get("input", {}))
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
    if texts:
        return "\n".join(texts)
    raise ProviderError("anthropic response contained no tool_use or text block")


def _usage(body: dict[str, Any]) -> dict[str, Any]:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "model": body.get("model"),
        "stop_reason": body.get("stop_reason"),
    }
