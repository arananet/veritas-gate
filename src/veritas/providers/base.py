"""Model provider abstraction.

Providers are interchangeable: a judge asks for a structured object matching a
Pydantic schema and does not know which vendor answered.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ValidationError

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce a usable structured response."""


class MissingCredentialsError(ProviderError):
    """Raised when the API key environment variable for a provider is unset."""


@dataclass(slots=True)
class ModelSpec:
    """Resolved model settings for one judge role."""

    provider: str
    model: str
    temperature: float | None = None
    max_tokens: int = 16000
    timeout: float = 120.0
    max_retries: int = 3
    base_url: str | None = None
    api_key_env: str | None = None
    tls_verify: bool | str = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StructuredResponse:
    """A parsed model response plus the usage metadata worth recording."""

    value: BaseModel
    usage: dict[str, Any] = field(default_factory=dict)
    raw: str = ""


@runtime_checkable
class ModelProvider(Protocol):
    """Interface every provider adapter implements."""

    name: str

    async def generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[SchemaT],
    ) -> StructuredResponse: ...


def json_schema_for(schema: type[BaseModel]) -> dict[str, Any]:
    """Return a strict JSON schema for ``schema`` with vendor-hostile bits removed."""
    raw = schema.model_json_schema()
    inlined = _inline_defs(raw, raw.get("$defs", {}))
    assert isinstance(inlined, dict)
    return inlined


def _inline_defs(node: Any, defs: dict[str, Any]) -> Any:
    """Inline ``$ref`` entries; several providers reject ``$ref``/``$defs``."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref = node["$ref"]
            key = ref.rsplit("/", 1)[-1]
            target = defs.get(key, {})
            merged = {k: v for k, v in node.items() if k != "$ref"}
            return _inline_defs({**target, **merged}, defs)
        result = {}
        for key, value in node.items():
            if key == "$defs":
                continue
            result[key] = _inline_defs(value, defs)
        if result.get("type") == "object":
            result.setdefault("additionalProperties", False)
            properties = result.get("properties")
            if isinstance(properties, dict):
                result["required"] = list(properties)
        return result
    if isinstance(node, list):
        return [_inline_defs(item, defs) for item in node]
    return node


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_structured[T: BaseModel](text: str, schema: type[T]) -> T:
    """Parse ``text`` into ``schema``, tolerating fences and surrounding prose.

    Malformed model output is a normal operating condition, not a crash: the
    caller turns a :class:`ProviderError` into an error result so the run
    continues with the remaining judges.
    """
    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if not isinstance(payload, dict):
            last_error = ValueError("expected a JSON object")
            continue
        try:
            return schema.model_validate(payload)
        except ValidationError as exc:
            last_error = exc
        # A model sometimes returns a list or object field as a JSON *string* --
        # the whole array, correctly formed, inside quotes. The content is
        # intact, and discarding it wastes the call and a judge's coverage.
        relaxed = _decode_stringified_fields(payload, schema)
        if relaxed is not payload:
            try:
                return schema.model_validate(relaxed)
            except ValidationError as exc:
                last_error = exc
        continue
    raise ProviderError(
        f"could not parse a {schema.__name__} from the model response: {last_error}"
    )


def _expects_structure(annotation: Any) -> bool:
    """Does this field want a list or an object rather than a string?"""
    text = str(annotation)
    if "str" in text and "list" not in text and "dict" not in text:
        return False
    return "list" in text or "dict" in text or "List" in text or "Dict" in text


def _decode_stringified_fields(payload: dict[str, Any], schema: type[BaseModel]) -> dict[str, Any]:
    """Decode top-level fields sent as JSON strings where the schema wants structure.

    The schema decides, so the parser never guesses what a value was meant to
    be: a field declared as a string keeps its text, even text that looks like
    JSON.
    """
    fields = schema.model_fields
    changed = False
    relaxed = dict(payload)
    for name, value in payload.items():
        field = fields.get(name)
        if field is None or not isinstance(value, str):
            continue
        if not _expects_structure(field.annotation):
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, list | dict):
            relaxed[name] = decoded
            changed = True
    return relaxed if changed else payload
