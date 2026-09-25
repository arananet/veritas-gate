"""Shared HTTP plumbing for provider adapters: retries, timeouts, backoff."""

from __future__ import annotations

import asyncio
import os
import random
import re
import ssl
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from veritas.providers.base import MissingCredentialsError, ModelSpec, ProviderError

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}

# A 429 usually means "slow down", but some are permanent: an exhausted balance
# or a spent quota will not clear during a backoff, so retrying only delays the
# error the user needs to see.
PERMANENT_429_MARKERS = (
    "insufficient_quota",
    "credit_balance_exhausted",
    "billing_hard_limit_reached",
    "no credits remaining",
    "exceeded your current quota",
)


def _is_permanent(status: int, body: str) -> bool:
    if status != 429:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in PERMANENT_429_MARKERS)


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
        # YAML interpolated from the environment arrives as a string, so the
        # booleans have to be recognised here too: `tls_verify: ${VERITAS_TLS_VERIFY:-true}`
        # would otherwise be read as a path to a file named "true".
        if verify.lower() in ("true", "1", "yes", "on", "default", "certifi"):
            return True
        if verify.lower() in ("false", "0", "no", "off"):
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
                if response.status_code not in RETRYABLE_STATUS or _is_permanent(
                    response.status_code, detail
                ):
                    raise ProviderError(
                        f"{spec.provider} request failed ({response.status_code}): {detail}"
                    )
                last_error = ProviderError(
                    f"{spec.provider} request failed ({response.status_code}): {detail}"
                )
                stated = _stated_wait(response.headers, detail)
                if attempt < attempts - 1:
                    await asyncio.sleep(stated if stated is not None else _backoff(attempt))
                continue
            if attempt < attempts - 1:
                await asyncio.sleep(_backoff(attempt))
    raise ProviderError(f"{spec.provider} request failed after {attempts} attempts: {last_error}")


MAX_STATED_WAIT = 120.0
_TRY_AGAIN = re.compile(r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|s)\b", re.IGNORECASE)


def _stated_wait(headers: Any, body: str) -> float | None:
    """How long the provider asked us to wait, if it said.

    A tokens-per-minute 429 names its wait -- "try again in 33.024s" -- and the
    old backoff, capped at eight seconds, spent every attempt before the limit
    cleared. Honouring the stated wait turns a busy minute into a pause instead
    of a failed judge. Capped, so a stuck provider still surfaces as an error.
    """
    seconds: float | None = None
    try:
        ms = headers.get("retry-after-ms")
        if ms:
            seconds = float(ms) / 1000
        elif headers.get("retry-after"):
            seconds = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        seconds = None
    if seconds is None:
        match = _TRY_AGAIN.search(body or "")
        if match:
            value = float(match.group(1))
            seconds = value / 1000 if match.group(2).lower() == "ms" else value
    if seconds is None:
        return None
    return min(MAX_STATED_WAIT, max(0.0, seconds) + 1.0)


def _backoff(attempt: int) -> float:
    return min(8.0, 2.0**attempt) * (0.5 + random.random() / 2)
