"""Google Gemini generateContent adapter.

Structured output uses ``responseMimeType: application/json`` together with a
``responseSchema``, which Gemini enforces server-side.
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

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Gemini's schema dialect is a subset of JSON Schema.
_UNSUPPORTED_SCHEMA_KEYS = {
    "$schema",
    "additionalProperties",
    "title",
    "default",
    "examples",
    "const",
    "exclusiveMinimum",
    "exclusiveMaximum",
}


class GoogleProvider:
    """Provider adapter for Google Gemini models."""

    name = "google"

    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[SchemaT],
    ) -> StructuredResponse:
        base = (self.spec.base_url or DEFAULT_BASE_URL).rstrip("/")
        api_key = require_api_key(self.spec, "GOOGLE_API_KEY")
        headers = {"content-type": "application/json", "x-goog-api-key": api_key}
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "maxOutputTokens": self.spec.max_tokens,
                "responseMimeType": "application/json",
                "responseSchema": _gemini_schema(json_schema_for(schema)),
            },
        }
        if self.spec.temperature is not None:
            payload["generationConfig"]["temperature"] = self.spec.temperature
        payload.update(self.spec.extra)

        url = f"{base}/models/{self.spec.model}:generateContent"
        body = await post_json(self.spec, url, headers=headers, payload=payload)
        raw = _extract_text(body)
        value = parse_structured(raw, schema)
        return StructuredResponse(value=value, usage=_usage(body), raw=raw)


def _gemini_schema(node: Any) -> Any:
    if isinstance(node, dict):
        cleaned = {
            key: _gemini_schema(value)
            for key, value in node.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYS
        }
        # Gemini expects a single concrete type; collapse nullable unions.
        any_of = cleaned.pop("anyOf", None)
        if isinstance(any_of, list):
            concrete = [item for item in any_of if item.get("type") != "null"]
            if concrete:
                merged = dict(concrete[0])
                merged.update({k: v for k, v in cleaned.items() if k != "type"})
                merged["nullable"] = len(concrete) != len(any_of)
                return merged
        if isinstance(cleaned.get("type"), str):
            cleaned["type"] = cleaned["type"].upper()
        return cleaned
    if isinstance(node, list):
        return [_gemini_schema(item) for item in node]
    return node


def _extract_text(body: dict[str, Any]) -> str:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        feedback = body.get("promptFeedback")
        raise ProviderError(f"google response contained no candidates (feedback={feedback})")
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise ProviderError("google response contained no content parts")
    text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
    if not text.strip():
        raise ProviderError("google response text was empty")
    return text


def _usage(body: dict[str, Any]) -> dict[str, Any]:
    usage = body.get("usageMetadata")
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": usage.get("promptTokenCount"),
        "output_tokens": usage.get("candidatesTokenCount"),
        "total_tokens": usage.get("totalTokenCount"),
        "model": body.get("modelVersion"),
    }
