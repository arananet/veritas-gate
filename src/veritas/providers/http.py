"""Shared HTTP plumbing for provider adapters: retries, timeouts, backoff."""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any

import httpx

from veritas.providers.base import MissingCredentialsError, ModelSpec, ProviderError

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def require_api_key(spec: ModelSpec, default_env: str) -> str:
    """Read the provider API key from the environment. Secrets never live in config."""
    env_name = spec.api_key_env or default_env
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise MissingCredentialsError(
            f"{env_name} is not set; export it (see .env.example) to use provider "
            f"'{spec.provider}' with model '{spec.model}'."
        )
    return key


async def post_json(
    spec: ModelSpec,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST ``payload`` and return the decoded JSON body, retrying transient failures."""
    attempts = max(1, spec.max_retries)
    last_error: Exception | None = None
    timeout = httpx.Timeout(spec.timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(attempts):
            try:
                response = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code < 400:
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise ProviderError(
                            f"{spec.provider} returned a non-JSON body: {response.text[:400]}"
                        ) from exc
                    if not isinstance(body, dict):
                        raise ProviderError(f"{spec.provider} returned an unexpected JSON shape")
                    return body
                detail = response.text[:600]
                if response.status_code not in RETRYABLE_STATUS:
                    raise ProviderError(
                        f"{spec.provider} request failed ({response.status_code}): {detail}"
                    )
                last_error = ProviderError(
                    f"{spec.provider} request failed ({response.status_code}): {detail}"
                )
            if attempt < attempts - 1:
                await asyncio.sleep(_backoff(attempt))
    raise ProviderError(f"{spec.provider} request failed after {attempts} attempts: {last_error}")


def _backoff(attempt: int) -> float:
    return min(8.0, 2.0**attempt) * (0.5 + random.random() / 2)
