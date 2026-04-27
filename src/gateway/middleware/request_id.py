"""
Request ID middleware — raw ASGI.

Assigns a unique ``request_id`` to every HTTP request.  Runs FIRST in the
middleware stack so all downstream log lines carry the ID.

Behaviour:
  - If ``X-Request-Id`` header present and non-empty → reuse it.
  - Otherwise → generate ``req_<26 lowercase hex chars>``.
  - Stash in ``scope["state"]["request_id"]``.
  - Bind to structlog contextvars (visible on all log lines for this request).
  - Echo back in ``X-Request-Id`` response header.
  - Clear contextvars on request exit (prevent bleed between requests).

Skip-list: /healthz, /readyz, /metrics, /_internal/metrics — no request_id
overhead on probe or scrape paths.
"""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from typing import cast

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

_SKIP_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics", "/_internal/metrics"})


def _generate_request_id() -> str:
    """Return ``req_<26 lowercase hex chars>`` (104 bits of randomness)."""
    return "req_" + os.urandom(13).hex()


class RequestIDMiddleware:
    """Raw ASGI middleware: assign + propagate request_id."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        # Resolve or generate request_id.
        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        request_id: str = ""
        for name, value in headers:
            if name.lower() == b"x-request-id":
                candidate = value.decode("latin-1", errors="replace").strip()
                if candidate:
                    request_id = candidate
                break
        if not request_id:
            request_id = _generate_request_id()

        # Stash in scope state and structlog contextvars.
        state: dict[str, object] = scope.setdefault("state", {})
        state["request_id"] = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # Wrap send to inject X-Request-Id response header.
        async def send_with_request_id(message: MutableMapping[str, object]) -> None:
            if message["type"] == "http.response.start":
                existing = cast(list[tuple[bytes, bytes]], message.get("headers", []))
                resp_headers: list[tuple[bytes, bytes]] = list(existing)
                resp_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": resp_headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # Clean up so request_id never bleeds into the next request on the
            # same event-loop task (e.g. keep-alive HTTP/1.1 pipelining).
            structlog.contextvars.unbind_contextvars("request_id")
