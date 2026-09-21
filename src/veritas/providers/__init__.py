"""Model providers. Swapping vendors is configuration, never a code change."""

from __future__ import annotations

from veritas.providers.anthropic import AnthropicProvider
from veritas.providers.base import (
    MissingCredentialsError,
    ModelProvider,
    ModelSpec,
    ProviderError,
    StructuredResponse,
)
from veritas.providers.google import GoogleProvider
from veritas.providers.openai import OpenAICompatibleProvider, OpenAIProvider

PROVIDERS: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "openai-compatible": OpenAICompatibleProvider,
    "google": GoogleProvider,
}

__all__ = [
    "PROVIDERS",
    "AnthropicProvider",
    "GoogleProvider",
    "MissingCredentialsError",
    "ModelProvider",
    "ModelSpec",
    "OpenAICompatibleProvider",
    "OpenAIProvider",
    "ProviderError",
    "StructuredResponse",
    "build_provider",
]


def build_provider(spec: ModelSpec) -> ModelProvider:
    """Instantiate the adapter named by ``spec.provider``."""
    try:
        factory = PROVIDERS[spec.provider]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise ProviderError(
            f"unknown provider '{spec.provider}'; available providers: {known}"
        ) from None
    provider = factory(spec)
    return provider  # type: ignore[no-any-return]
