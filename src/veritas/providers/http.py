"""Shared HTTP plumbing for provider adapters: retries, timeouts, backoff."""

from __future__ import annotations

import asyncio
import os
import random
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from veritas.providers.base import MissingCredentialsError, ModelSpec, ProviderError

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

_INSECURE_WARNED: set[str] = set()


class TLSConfigurationError(ProviderError):
    """Raised when the configured TLS verification cannot be set up."""


def build_verify(spec: ModelSpec) -> Any:
    """Turn ``tls_verify`` into something httpx accepts.

    Behind a TLS-intercepting proxy the fix is to trust the proxy's CA, not to
    stop checking certificates: the connection carries an API key and the whole
    artifact being evaluated.
    """
    verify = spec.tls_verify

    if verify is True:
        return True

    if verify is False:
        _warn_insecure(spec)
        return False

    if isinstance(verify, str):
        if verify == "truststore":
            return _truststore_context()
        if verify in ("default", "certifi"):
            return True
        if verify in ("false", "no", "off"):
            # Only reachable from hand-written YAML; treat it as the opt-out.
            _warn_insecure(spec)
            return False
        path = Path(verify).expanduser()
        if not path.exists():
            raise TLSConfigurationError(
                f"tls_verify points at '{path}', which does not exist. Give the path to a "
                "CA bundle, or use tls_verify: truststore to use the system trust store."
            )
        return str(path)

    raise TLSConfigurationError(f"tls_verify has an unsupported value: {verify!r}")


def _truststore_context() -> ssl.SSLContext:
    try:
        import truststore
    except ModuleNotFoundError as exc:
        raise TLSConfigurationError(
            "tls_verify: truststore requires the truststore package. "
            'Install it with: pip install "veritas-gate[tls]"'
        ) from exc
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _warn_insecure(spec: ModelSpec) -> None:
    """Say it out loud, once per endpoint. Silent insecurity is the dangerous kind."""
    target = spec.base_url or spec.provider
    if target in _INSECURE_WARNED:
        return
    _INSECURE_WARNED.add(target)
    host = urlparse(spec.base_url or "").hostname or ""
    scope = (
        "This is a local endpoint."
        if host in ("localhost", "127.0.0.1", "::1")
        else "This is NOT a local endpoint: your API key and the artifact being "
        "evaluated are exposed to anyone on the network path."
    )
    print(
        f"warning: TLS certificate verification is disabled for {target}. {scope}",
        file=sys.stderr,
    )


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
    async with httpx.AsyncClient(timeout=timeout, verify=build_verify(spec)) as client:
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
