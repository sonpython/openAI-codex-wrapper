"""
Observability middleware — raw ASGI.

Runs after RequestIDMiddleware and before EdgeIPLimiter so every request is
timed and counted, including those rejected by rate-limiting or auth.

Responsibilities:
  1. Record wall-clock duration via time.monotonic().
  2. Capture HTTP status_code by wrapping send().
  3. Increment ``http_requests_total{route, status, method}``.
  4. Observe ``http_request_duration_seconds{route}``.
  5. Emit ``request.completed`` structlog line with route/status/duration_ms.
  6. Emit ``request.failed`` at ERROR level on 5xx responses.

Route template: use ``scope["route"]`` (set by Starlette router) when
available; fall back to raw ``scope["path"]`` to avoid high-cardinality
labels from parameterised paths.

Skip-list: /healthz, /readyz, /metrics, /_internal/metrics.
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from typing import cast

import structlog
from starlette.routing import Match
from starlette.types import ASGIApp, Receive, Scope, Send

from src.observability.metrics import HTTP_DURATION, HTTP_REQUESTS

logger = structlog.get_logger(__name__)

_SKIP_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz", "/metrics", "/_internal/metrics"})


def _route_template(scope: Scope) -> str:
    """Extract the route template from the matched Starlette route.

    Falls back to the raw path when the router has not yet matched (e.g.
    404 paths) to avoid cardinality explosion from URL parameters.
    """
    # Starlette populates scope["route"] after routing resolves.
    route = scope.get("route")
    if route is not None and hasattr(route, "path"):
        return str(route.path)

    # Attempt to match against app routes for proper template extraction.
    app = scope.get("app")
    if app is not None and hasattr(app, "routes"):
        for r in app.routes:
            match, _ = r.matches(scope)
            if match == Match.FULL and hasattr(r, "path"):
                return str(r.path)

    return scope.get("path", "unknown")


class ObservabilityMiddleware:
    """Raw ASGI middleware: emit Prometheus metrics and structured log per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if path in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "UNKNOWN")
        start = time.monotonic()
        status_code: int = 0

        async def send_capture_status(message: MutableMapping[str, object]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(cast(int, message.get("status", 0)))
            await send(message)

        try:
            await self.app(scope, receive, send_capture_status)
        finally:
            duration = time.monotonic() - start
            duration_ms = int(duration * 1000)

            # Route template resolved after routing has run.
            route = _route_template(scope)
            status_str = str(status_code) if status_code else "0"

            HTTP_REQUESTS.labels(route=route, status=status_str, method=method).inc()
            HTTP_DURATION.labels(route=route).observe(duration)

            state: dict[str, object] = scope.get("state", {})
            request_id = state.get("request_id", "")

            log_fields = {
                "route": route,
                "method": method,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "request_id": request_id,
            }

            if status_code >= 500:
                logger.error("request.failed", **log_fields)
            else:
                logger.info("request.completed", **log_fields)
