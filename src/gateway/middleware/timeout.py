"""
Raw ASGI timeout middleware.

Wraps each request in asyncio.wait_for() with a per-route timeout.
On TimeoutError: returns 504 Gateway Timeout with OpenAI-shaped JSON.

Route dispatch table (path prefix → seconds):
  /v1/chat/completions    → settings.chat_default_timeout_seconds (120)
  /v1/responses           → settings.responses_timeout_seconds (120)
  /v1/codex/jobs/{id}/events → no timeout (long-lived SSE stream)
  /healthz /readyz /_internal → no timeout (skipped)
  everything else         → 30s default

The middleware is inserted BEFORE ObservabilityMiddleware so 504s are
also tracked in Prometheus request counts.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from src.settings import get_settings

logger = structlog.get_logger(__name__)

# Routes that should never be timed out
_NO_TIMEOUT_PREFIXES = (
    "/healthz",
    "/readyz",
    "/_internal",
)

# SSE stream path — no timeout (rely on keepalive)
_SSE_SUFFIX = "/events"


def _get_timeout(path: str, settings: Any) -> float | None:
    """Return timeout in seconds for the given path, or None for no timeout."""
    # Skip health + metrics
    for prefix in _NO_TIMEOUT_PREFIXES:
        if path.startswith(prefix):
            return None

    # Long-lived SSE stream — no timeout
    if path.endswith(_SSE_SUFFIX):
        return None

    # Per-route table
    if path.startswith("/v1/chat/completions"):
        return float(settings.chat_default_timeout_seconds)
    if path.startswith("/v1/responses"):
        return float(settings.responses_timeout_seconds)

    # Default: 30s for everything else
    return 30.0


def _timeout_response(route: str) -> list[Any]:
    """Build ASGI 504 response bytes."""
    body = json.dumps(
        {
            "error": {
                "message": f"Request timed out on route {route}",
                "type": "timeout",
                "code": "timeout",
                "param": None,
            }
        }
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    return [
        {
            "type": "http.response.start",
            "status": 504,
            "headers": headers,
        },
        {
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        },
    ]


class TimeoutMiddleware:
    """Raw ASGI middleware enforcing per-route timeouts.

    Inserted between RequestIDMiddleware and ObservabilityMiddleware so
    504 responses are tracked by observability metrics.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        settings = get_settings()
        timeout = _get_timeout(path, settings)

        if timeout is None:
            await self.app(scope, receive, send)
            return

        try:
            await asyncio.wait_for(
                self.app(scope, receive, send),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning(
                "request.timeout",
                path=path,
                timeout_seconds=timeout,
            )
            for message in _timeout_response(path):
                await send(message)
