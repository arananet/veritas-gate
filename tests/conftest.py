"""Shared fixtures.

Provider adapters are exercised against a real local HTTP server rather than a
patched client, so the request payloads and response parsing under test are the
ones that would go over the wire. No API keys and no network are required.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

Handler = Callable[[str, dict[str, Any]], tuple[int, dict[str, Any]]]


class RecordingServer:
    """A local HTTP server that records requests and replies from a handler."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append((self.path, body, dict(self.headers)))
                status, payload = outer.handler(self.path, body)
                encoded = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    # The client gave up (a retry test, or a closed pool). Left
                    # unhandled this surfaces as a stray unraisable-exception
                    # warning that has nothing to do with the code under test.
                    self.close_connection = True

            def handle_one_request(self) -> None:
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

            def log_message(self, *args: object) -> None:
                return

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> RecordingServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"


@pytest.fixture
def serve() -> Callable[[Handler], RecordingServer]:
    """Return a factory for local HTTP servers used as provider endpoints."""
    return RecordingServer


JUDGE_PAYLOAD: dict[str, Any] = {
    "status": "fail",
    "summary": "The document overstates what its data supports.",
    "findings": [
        {
            "title": "Headline improvement is unsupported",
            "severity": "major",
            "category": "evidence",
            "description": "The reported improvement is not backed by the supplied results.",
            "location": "paper/main.md#results",
            "evidence": ["Results table reports 41ms", "benchmark.json reports 48.3ms"],
            "recommendation": "Reconcile the reported number with the artifact.",
            "confidence": 0.9,
        },
        {
            "title": "Terminology is inconsistent",
            "severity": "minor",
            "category": "clarity",
            "description": "Two different names are used for the same component.",
            "location": "paper/main.md#method",
            "evidence": ["'router' in Method", "'dispatcher' in Results"],
            "confidence": 0.5,
        },
    ],
    "claims": [
        {
            "text": "The method reduces p99 latency by 37%.",
            "source_location": "paper/main.md#abstract",
            "importance": "major",
            "status": "unsupported",
            "evidence": ["benchmark.json reports a 25.7% reduction"],
            "rationale": "The artifact contradicts the stated figure.",
        },
        {
            "text": "Throughput is unchanged.",
            "source_location": "paper/main.md#results",
            "importance": "minor",
            "status": "verified",
            "evidence": ["benchmark.json records equal throughput for both routers"],
        },
    ],
}


@pytest.fixture
def judge_payload() -> dict[str, Any]:
    return json.loads(json.dumps(JUDGE_PAYLOAD))


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
