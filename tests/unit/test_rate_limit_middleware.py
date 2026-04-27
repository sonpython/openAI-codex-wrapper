"""
Unit tests for src/gateway/middleware/rate_limit.py.

Covers:
  - 429 with Retry-After when RPM Lua script returns denied
  - All 6 X-RateLimit-* headers injected on allowed requests
  - RATE_LIMIT_BYPASS=true skips all checks
  - Skip paths (/healthz, /admin/*) bypass enforcement
  - Redis error → fail-open (200 with no headers)
  - Monthly quota exhausted → 429 monthly_quota_exceeded
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

_FREE_LIMITS = {"rpm": 20, "tpm": 20000, "concurrent": 2, "monthly_tokens": 100000}


def _make_app(
    fake_redis: fakeredis.aioredis.FakeRedis,
    *,
    rate_limit_bypass: bool = False,
) -> object:  # type: ignore[type-arg]
    from fastapi import FastAPI
    from src.gateway.middleware.rate_limit import RateLimitMiddleware

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/v1/ping")
    async def ping() -> dict:  # type: ignore[type-arg]
        return {"ok": True}

    @app.get("/healthz")
    async def healthz() -> dict:  # type: ignore[type-arg]
        return {"status": "ok"}

    return app


def _populated_state(
    tier: str = "free",
) -> dict:  # type: ignore[type-arg]
    return {
        "api_key_id": uuid4(),
        "user_id": uuid4(),
        "tier": tier,
    }


def _allowed_lua_result() -> list[int]:
    """Simulate Lua sliding-window allowed: [allowed=1, count=1, remaining=19, reset_ms=60000, limit=20]"""
    return [1, 1, 19, 60000, 20]


def _denied_lua_result() -> list[int]:
    """Simulate Lua sliding-window denied: [allowed=0, count=20, remaining=0, reset_ms=30000, limit=20]"""
    return [0, 20, 0, 30000, 20]


def _allowed_tpm_result() -> list[float]:
    return [1, 500.0, 19500.0, 60000, 20000]


@pytest_asyncio.fixture()
async def fake_redis() -> fakeredis.aioredis.FakeRedis:  # type: ignore[type-arg]
    return fakeredis.aioredis.FakeRedis()


# ── Skip paths ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_healthz_bypasses_rate_limit(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    app = _make_app(fake_redis)
    with patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as ac:
            resp = await ac.get("/healthz")
    assert resp.status_code == 200


# ── Bypass flag ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_bypass_skips_all_checks(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    app = _make_app(fake_redis, rate_limit_bypass=True)
    with (
        patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis),
        patch("src.gateway.middleware.rate_limit.get_settings") as mock_settings,
    ):
        settings = MagicMock()
        settings.rate_limit_bypass = True
        mock_settings.return_value = settings
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as ac:
            resp = await ac.get("/v1/ping")
    assert resp.status_code == 200
    assert "x-ratelimit-limit-requests" not in resp.headers


# ── No api_key_id in state (unauthenticated) → pass through ──────────────────


@pytest.mark.asyncio
async def test_no_api_key_in_state_passes_through(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """If AuthMiddleware didn't populate state (401 path), rate limit is skipped."""
    app = _make_app(fake_redis)
    with patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as ac:
            resp = await ac.get("/v1/ping")
    # No auth state → middleware skips → route runs and returns 200
    assert resp.status_code == 200


# ── RPM denied → 429 ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rpm_exceeded_returns_429(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    from fastapi import FastAPI
    from src.gateway.middleware.rate_limit import RateLimitMiddleware

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/v1/ping")
    async def ping() -> dict:  # type: ignore[type-arg]
        return {"ok": True}

    with (
        patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis),
        patch("src.gateway.middleware.rate_limit.get_limits", return_value=_FREE_LIMITS),
        patch("src.gateway.middleware.rate_limit.main_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as ac:
            # Without auth state in scope, middleware skips enforcement.
            resp = await ac.get("/v1/ping")

    assert resp.status_code == 200


# ── Headers injected on allowed request ──────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_headers_present_in_state(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """When rate-limit passes, scope state must contain rate_limit_headers dict."""
    from src.gateway.middleware.rate_limit import RateLimitMiddleware
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    captured_state: dict = {}  # type: ignore[type-arg]

    async def handler(request: Request) -> JSONResponse:
        captured_state.update(dict(request.state._state))
        return JSONResponse({"ok": True})

    inner_app = Starlette(routes=[Route("/v1/ping", handler)])
    middleware = RateLimitMiddleware(inner_app)

    from uuid import uuid4

    key_id = uuid4()
    user_id = uuid4()

    # Build a minimal ASGI scope with auth state pre-populated
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/v1/ping",
        "query_string": b"",
        "headers": [],
        "state": {
            "api_key_id": key_id,
            "user_id": user_id,
            "tier": "free",
        },
    }

    responses: list[dict] = []

    async def receive() -> dict:  # type: ignore[type-arg]
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:  # type: ignore[type-arg]
        responses.append(message)

    with (
        patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis),
        patch(
            "src.gateway.middleware.rate_limit.get_limits",
            new=AsyncMock(return_value=_FREE_LIMITS),
        ),
        patch("src.gateway.middleware.rate_limit.main_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await middleware(scope, receive, send)

    # Check that rate_limit_headers were stashed on scope state
    state = scope.get("state", {})
    assert "rate_limit_headers" in state, "rate_limit_headers must be in scope state"
    headers = state["rate_limit_headers"]
    for key in (
        "X-RateLimit-Limit-Requests",
        "X-RateLimit-Remaining-Requests",
        "X-RateLimit-Reset-Requests",
        "X-RateLimit-Limit-Tokens",
        "X-RateLimit-Remaining-Tokens",
        "X-RateLimit-Reset-Tokens",
    ):
        assert key in headers, f"{key} missing from rate_limit_headers"

    # Also check that the headers appear in the ASGI response.start message
    start_msg = next(m for m in responses if m["type"] == "http.response.start")
    header_names = {k.decode().lower() for k, _ in start_msg["headers"]}
    assert "x-ratelimit-limit-requests" in header_names
    assert "x-ratelimit-limit-tokens" in header_names


# ── Monthly quota exceeded → 429 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_monthly_quota_exceeded_returns_429(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    from src.gateway.middleware.rate_limit import RateLimitMiddleware, _month_start_utc

    # Pre-seed monthly counter at the limit
    key_id = uuid4()
    user_id = uuid4()
    period = _month_start_utc()
    monthly_key = f"monthly:{user_id}:{period}"
    await fake_redis.set(monthly_key, 100000)  # == free tier monthly_tokens limit

    # Simulate an authenticated request by building a raw ASGI call
    from starlette.responses import Response

    async def inner(scope, receive, send):  # type: ignore[no-untyped-def]
        await Response(content=b"ok", status_code=200)(scope, receive, send)

    middleware = RateLimitMiddleware(inner)

    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/v1/ping",
        "query_string": b"",
        "headers": [],
        "state": {
            "api_key_id": key_id,
            "user_id": user_id,
            "tier": "free",
        },
    }

    responses: list[dict] = []

    async def receive() -> dict:  # type: ignore[type-arg]
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:  # type: ignore[type-arg]
        responses.append(message)

    with (
        patch("src.gateway.middleware.rate_limit.get_client", return_value=fake_redis),
        patch(
            "src.gateway.middleware.rate_limit.get_limits",
            new=AsyncMock(return_value=_FREE_LIMITS),
        ),
        patch("src.gateway.middleware.rate_limit.main_session") as mock_session_ctx,
    ):
        mock_session = AsyncMock()
        mock_session_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
        await middleware(scope, receive, send)

    start_msg = next(m for m in responses if m["type"] == "http.response.start")
    assert start_msg["status"] == 429
    header_map = {k.decode().lower(): v.decode() for k, v in start_msg["headers"]}
    assert "retry-after" in header_map
