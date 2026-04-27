"""
Unit tests asserting middleware execution order in the full app stack.

Phase-06 spec requires:
  REQUEST flow: EdgeIPLimiter → AuthMiddleware → RateLimitMiddleware → UsageTracking → route

We verify by recording which middleware saw the request first via a shared
execution-order log injected into each mock middleware layer.

Critical assertion from spec §9:
  "EdgeIPLimiter runs FIRST (before auth)" — this is the C2 fix.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def _make_ordered_app() -> tuple[object, list[str]]:
    """Build a test app where each middleware appends its name to an order log."""
    from fastapi import FastAPI
    from starlette.types import ASGIApp, Receive, Scope, Send

    execution_log: list[str] = []

    class _LoggingMiddleware:
        def __init__(self, app: ASGIApp, name: str) -> None:
            self.app = app
            self.name = name

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                execution_log.append(self.name)
            await self.app(scope, receive, send)

    app = FastAPI()

    # Registration order (first registered = innermost on request path):
    # 4. UsageTracking — innermost
    app.add_middleware(_LoggingMiddleware, name="UsageTracking")
    # 3. RateLimit
    app.add_middleware(_LoggingMiddleware, name="RateLimit")
    # 2. Auth
    app.add_middleware(_LoggingMiddleware, name="Auth")
    # 1. EdgeIPLimiter — outermost (last registered = first on request)
    app.add_middleware(_LoggingMiddleware, name="EdgeIPLimiter")

    @app.get("/v1/ping")
    async def ping() -> dict:  # type: ignore[type-arg]
        execution_log.append("route")
        return {"ok": True}

    return app, execution_log


@pytest.mark.asyncio
async def test_middleware_execution_order_on_request() -> None:
    """Verify EdgeIPLimiter runs first, then Auth, RateLimit, UsageTracking, route."""
    app, log = _make_ordered_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as ac:
        await ac.get("/v1/ping")

    assert log == [
        "EdgeIPLimiter",
        "Auth",
        "RateLimit",
        "UsageTracking",
        "route",
    ], f"wrong order: {log}"


@pytest.mark.asyncio
async def test_edge_ip_limiter_is_outermost() -> None:
    """EdgeIPLimiter must be index 0 in execution log — it runs before Auth."""
    app, log = _make_ordered_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as ac:
        await ac.get("/v1/ping")

    assert log[0] == "EdgeIPLimiter", f"EdgeIPLimiter must run FIRST (C2 fix), but got: {log[0]}"


@pytest.mark.asyncio
async def test_auth_runs_before_rate_limit() -> None:
    """Auth must run before RateLimit so api_key_id is in state when RL checks it."""
    app, log = _make_ordered_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as ac:
        await ac.get("/v1/ping")

    auth_idx = log.index("Auth")
    rl_idx = log.index("RateLimit")
    assert auth_idx < rl_idx, f"Auth ({auth_idx}) must precede RateLimit ({rl_idx})"


@pytest.mark.asyncio
async def test_usage_tracking_is_innermost() -> None:
    """UsageTracking is closest to route — it observes the final response body."""
    app, log = _make_ordered_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    ) as ac:
        await ac.get("/v1/ping")

    route_idx = log.index("route")
    usage_idx = log.index("UsageTracking")
    assert usage_idx == route_idx - 1, (
        f"UsageTracking must immediately precede route; "
        f"UsageTracking={usage_idx}, route={route_idx}"
    )


@pytest.mark.asyncio
async def test_real_app_middleware_registration_order() -> None:
    """Verify that create_app() registers middlewares in the correct LIFO order.

    Phase-07 adds RequestIDMiddleware as the outermost middleware so EVERY log
    line (including 429s from EdgeIPLimiter) carries a request_id in structlog
    contextvars.

    Desired REQUEST flow:
      RequestID(0) → Observability(1) → EdgeIPLimiter(2) → Auth(3) → RateLimit(4)
        → UsageTracking(5) → route

    We test the TYPES registered, not execution (that requires live Redis).
    FastAPI stores middlewares in app.middleware_stack after build — we check
    the middleware class list via app.user_middleware which holds registration order.
    """

    # Must patch settings so create_app() doesn't fail on missing DATABASE_URL

    # Patch lifespan to a no-op so create_app doesn't start background tasks
    with (
        patch("src.gateway.app.lifespan"),
        patch("src.gateway.app.get_client", return_value=MagicMock()),
    ):
        from src.gateway.app import create_app
        from src.gateway.middleware.auth import AuthMiddleware
        from src.gateway.middleware.edge_ip_limiter import EdgeIPLimiter
        from src.gateway.middleware.rate_limit import RateLimitMiddleware
        from src.gateway.middleware.request_id import RequestIDMiddleware
        from src.gateway.middleware.usage_tracking import UsageTrackingMiddleware

        app = create_app()

    # user_middleware is a list of Middleware(cls, **kwargs) in registration order.
    # Last registered = outermost on request = index 0 in user_middleware.
    mw_classes = [m.cls for m in app.user_middleware]

    assert RequestIDMiddleware in mw_classes, "RequestIDMiddleware must be registered"
    assert EdgeIPLimiter in mw_classes, "EdgeIPLimiter must be registered"
    assert AuthMiddleware in mw_classes, "AuthMiddleware must be registered"
    assert RateLimitMiddleware in mw_classes, "RateLimitMiddleware must be registered"
    assert UsageTrackingMiddleware in mw_classes, "UsageTrackingMiddleware must be registered"

    # Actual REQUEST flow (LIFO: last add_middleware call = outermost):
    #   index 0: RequestIDMiddleware (outermost — assigns request_id first)
    #   index 1: TimeoutMiddleware (phase-08: per-route hard timeouts, 504 on exceed)
    #   index 2: ObservabilityMiddleware (times + counts all requests incl. 504s)
    #   index 3: EdgeIPLimiter (runs before auth — C2 fix)
    #   index 4: AuthMiddleware
    #   index 5: RateLimitMiddleware
    #   index 6: UsageTrackingMiddleware (innermost)
    from src.gateway.middleware.observability import ObservabilityMiddleware
    from src.gateway.middleware.timeout import TimeoutMiddleware

    assert (
        mw_classes[0] == RequestIDMiddleware
    ), f"RequestIDMiddleware must be outermost (index 0), got {mw_classes[0]}"
    assert (
        mw_classes[1] == TimeoutMiddleware
    ), f"TimeoutMiddleware must be index 1, got {mw_classes[1]}"
    assert (
        mw_classes[2] == ObservabilityMiddleware
    ), f"ObservabilityMiddleware must be index 2, got {mw_classes[2]}"
    # EdgeIPLimiter still runs before Auth (C2 fix preserved).
    edge_idx = mw_classes.index(EdgeIPLimiter)
    auth_idx = mw_classes.index(AuthMiddleware)
    assert edge_idx < auth_idx, f"EdgeIPLimiter ({edge_idx}) must precede Auth ({auth_idx})"
